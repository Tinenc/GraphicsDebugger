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

#include "AIAssistant.h"
#include "Windows/BufferViewer.h"
#include "Windows/TextureViewer.h"
#include "AIConfiguration.h"
#include "AIVisualReply.h"

#include <algorithm>
#include <cmath>
#include <functional>
#include <memory>
#include <cstring>
#include <utility>
#include <QApplication>
#include <QAction>
#include <QBrush>
#include <QBuffer>
#include <QButtonGroup>
#include <QCheckBox>
#include <QClipboard>
#include <QColor>
#include <QComboBox>
#include <QCommandLinkButton>
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QCursor>
#include <QDateTime>
#include <QDesktopServices>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDebug>
#include <QDragEnterEvent>
#include <QDragMoveEvent>
#include <QDir>
#include <QDropEvent>
#include <QElapsedTimer>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QSaveFile>
#include <QFontMetrics>
#include <QFormLayout>
#include <QFontDatabase>
#include <QGroupBox>
#include <QHeaderView>
#include <QHideEvent>
#include <QHBoxLayout>
#include <QIcon>
#include <QImage>
#include <QImageReader>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QKeyEvent>
#include <QLabel>
#include <QLineEdit>
#include <QList>
#include <QHash>
#include <QListWidget>
#include <QLocale>
#include <QMenu>
#include <QMessageBox>
#include <QMimeData>
#include <QMimeDatabase>
#include <QMouseEvent>
#include <QMovie>
#include <QPainterPath>
#include <QMutex>
#include <QMutexLocker>
#include <QPainter>
#include <QPalette>
#include <QPlainTextEdit>
#include <QSpinBox>
#include <QTabWidget>
#include <QPointer>
#include <QProcess>
#include <QProcessEnvironment>
#include <QPushButton>
#include <QPixmap>
#include <QRegularExpression>
#include <QResizeEvent>
#include <QScrollBar>
#include <QScrollArea>
#include <QShortcut>
#include <QSignalBlocker>
#include <QShowEvent>
#include <QSizePolicy>
#include <QStackedWidget>
#include <QStandardPaths>
#include <QStyle>
#include <QTextBrowser>
#include <QTextCodec>
#include <QTextLayout>
#include <QTextOption>
#include <QTemporaryFile>
#include <QThread>
#include <QThreadPool>
#include <QTimer>
#include "AIGuiHeartbeat.h"
#include <QToolButton>
#include <QTreeWidget>
#include <QUrl>
#include <QUrlQuery>
#include <QUuid>
#include <QVBoxLayout>
#include <QWheelEvent>
#include <QWidgetAction>
#include <QRunnable>
#include "Code/CaptureContext.h"
#include "Code/QRDUtils.h"
#include "Code/Resources.h"
#include "AIAgentMonitorWindow.h"
#include "AIAgentTraceStore.h"
#include "AIFileThumbnail.h"
#include "AIConversationListView.h"
#include "AIConversationStore.h"
#include "AIPendingChatView.h"
#include "Windows/Dialogs/SettingsDialog.h"

#if defined(Q_OS_WIN)
#include <QtCore/qt_windows.h>
#endif

static constexpr int SubscriptionStatusTimeoutMs = 30000;

struct AIConfigurationProcessState
{
  QPointer<AIAssistant> assistant;
  QPointer<QObject> owner;
  QPointer<QProcess> process;
  QPointer<QTimer> timeout;
  std::function<void(int, const QByteArray &, const QString &, bool)> completion;
  QByteArray input;
  QByteArray standardOutput;
  QByteArray standardError;
  QString forcedDiagnostic;
  QString operation;
  QElapsedTimer elapsed;
  int timeoutMs = 0;

  void log(const char *phase, int exitCode = -1) const
  {
    qInfo().nospace() << "AI configuration " << phase << " operation=" << operation
                      << " elapsedMs=" << elapsed.elapsed() << " timeoutMs=" << timeoutMs
                      << " processState=" << (process ? int(process->state()) : -1)
                      << " exitCode=" << exitCode;
  }
  void *job = NULL;
  void *processInformation = NULL;
  bool completed = false;

  void closeJob()
  {
#if defined(Q_OS_WIN)
    if(job)
      CloseHandle(static_cast<HANDLE>(job));
#endif
    job = NULL;
  }

  ~AIConfigurationProcessState()
  {
    // A Settings page may close while its asynchronous command is still running. The QObject
    // signal contexts then disappear before the ordinary completion callback can close the Job.
    // Keep the kill-on-close handle owned by this shared state as a final containment guard.
    closeJob();
  }
};

namespace
{
const qint64 MaxManifestBytes = 64 * 1024;
const qint64 MaxEventReadBytes = 256 * 1024;
const qint64 MaxEventReadBytesPerTask = 64 * 1024;
const int MaxEventRecordsPerTick = 128;
const qint64 MaxEventPollTimeMs = 4;
const int MaxEventLineBytes = 64 * 1024;
const qint64 MaxEventDrainBytes = 16 * 1024 * 1024;
const int MaxAnswerBytes = 2 * 1024 * 1024;
// Live raw reasoning shown inside the conversation's activity group; the Agent monitor keeps the
// complete stream.
const int MaxLiveReasoningChars = 256 * 1024;
const int MaxProcessOutputBytes = 8 * 1024 * 1024;
const int MaxPromptBytes = 16 * 1024 * 1024;
const int MaxContextSnapshotBytes = 512 * 1024;
const int MaxClarificationOtherChars = 16 * 1024;
const int MaxClarificationOriginChars = 256 * 1024;
const int MaxDiscoveredModels = 200;
const int MaxModelIdBytes = 512;
const int MaxConversationMessageChars = 256 * 1024;
const int MaxConversationMessages = 1000;
const int MaxConversationThreads = 32;
const int MaxPendingChats = 64;
const int MaxStoredConversationActivities = 256;
const int MaxChatAttachments = 8;
const int MaxAttachmentContentChars = 256 * 1024;
const int MaxAttachmentUTF8Bytes = 1024 * 1024;
const int MaxAttachmentTotalUTF8Bytes = 2 * 1024 * 1024;
const int MaxBinaryAttachmentBytes = 8 * 1024 * 1024;
const int MaxBinaryAttachmentTotalBytes = 8 * 1024 * 1024;
const int MaxImageDimension = 16384;
const qint64 MaxImagePixels = 64LL * 1024LL * 1024LL;
const int MaxAnimatedImageFrames = 2048;
const qint64 MaxAnimatedImagePixelFrames = 32LL * 1024LL * 1024LL;
const qint64 MaxPathReferenceImagePreviewBytes = 256LL * 1024LL * 1024LL;
const qint64 MaxPathReferenceAnimatedImageBytes = 64LL * 1024LL * 1024LL;
const qint64 MaxTextThumbnailReadBytes = 64LL * 1024LL;
const int MaxTextThumbnailChars = 1200;
const int MaxAttachmentPathChars = 32768;
const int MaxAttachmentContextBytes = 64 * 1024;
const qint64 MaxPathReferenceBytes = 32LL * 1024LL * 1024LL * 1024LL;
const qint64 MaxSafeJSONInteger = 9007199254740991LL;
const int MaxLiveConversationActivities = 512;
const int LiveConversationActivityTrimTarget = 384;
const int MaxActivityIdChars = 512;

int maxConcurrentAIChats()
{
  // AgentHost and its tools may each create their own worker threads. Keep the
  // number of independent process trees deliberately small, and on compact
  // machines allow only one so qrenderdoc retains foreground capacity.
  const int logicalProcessors = qMax(1, QThread::idealThreadCount());
  return logicalProcessors >= 8 ? 2 : 1;
}

QString clarificationOriginSha256(const QString &request)
{
  return QString::fromLatin1(
      QCryptographicHash::hash(request.toUtf8(), QCryptographicHash::Sha256).toHex());
}

bool clarificationOriginSha256IsValid(const QString &value)
{
  static const QRegularExpression digestPattern(
      lit("^[0-9a-f]{64}$"));    // i18n-ignore: SHA-256 protocol validation regex
  return digestPattern.match(value).hasMatch();
}
const int MaxLiveAgentMessageStreams = 128;
const int MaxLiveAgentMessageChars = 1024 * 1024;
const int TaskEventPollIntervalMs = 50;
const int ChatContextFreezePollIntervalMs = 200;
const qint64 ChatContextFreezeTimeoutMs = 60 * 1000;
const qint64 ChatBridgeReadinessTimeoutMs = 3 * 60 * 1000;
const int ChatBridgeReadinessPollIntervalMs = 1000;
const qint64 MaxContextFreezeResultBytes = 1024 * 1024;

bool isLiveTextActivityKind(const QString &kind)
{
  // i18n-ignore: Activity kinds and their per-plan suffixes are stable ReplayLab protocol IDs.
  return kind == QLatin1String("reasoning") || kind.startsWith(QLatin1String("reasoning-")) ||
         kind == QLatin1String("progressNote") ||
         kind.startsWith(QLatin1String("progressNote-")) ||
         kind == QLatin1String("child-message") ||
         kind.startsWith(QLatin1String("child-message-"));
}

bool publicAgentNarrativePath(const QStringList &path)
{
  // i18n-ignore: These paths are stable structured Agent-output protocol fields. Only exact
  // schema-qualified prose is public here; sibling IDs, hashes, routes, evidence receipts,
  // tool/function names and control metadata remain available in the Agent Monitor.
  static const QSet<QString> paths = {
      lit("answer"),                         lit("final_answer"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("reason"),                         lit("question"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("objective"),                      lit("deliverables/*"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("constraints/*"),                  lit("success_criteria/*"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("assumptions/*"),                  lit("ambiguities/*"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("stage_objective"),                lit("request_summary"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("tasks/*/summary"),                lit("goal/objective"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("goal/deliverables/*"),            lit("goal/constraints/*"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("goal/success_criteria/*"),         lit("goal/assumptions/*"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("goal/ambiguities/*"),             lit("goal/environment_summary"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("plan/request_summary"),           lit("plan/tasks/*/summary"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("options/*/label"),                lit("options/*/description"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("findings/*/claim"),               lit("findings/*/limitations/*"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("unknowns/*"),                     lit("limitations/*"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("evidence/*/summary"),             lit("coverage/*/note"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("analysis_obligation_closures/*/note"),    // i18n-ignore: stable structured Agent-output JSON path
      lit("details/observed_evidence/*"),     lit("details/inferences/*"),    // i18n-ignore: stable structured Agent-output JSON paths
      lit("details/algorithm_explanation"),    // i18n-ignore: stable structured Agent-output JSON path
  };
  return paths.contains(path.join(QLatin1Char('/')));
}

int publicAgentHexDigit(const QChar character)
{
  if(character >= QLatin1Char('0') && character <= QLatin1Char('9'))
    return character.unicode() - ushort('0');
  if(character >= QLatin1Char('a') && character <= QLatin1Char('f'))
    return character.unicode() - ushort('a') + 10;
  if(character >= QLatin1Char('A') && character <= QLatin1Char('F'))
    return character.unicode() - ushort('A') + 10;
  return -1;
}

bool publicAgentReplacementIdIsValid(const QString &value)
{
  if(value.size() != 64)
    return false;
  for(const QChar character : value)
  {
    if(publicAgentHexDigit(character) < 0)
      return false;
  }
  return true;
}

struct PublicAgentJsonString
{
  QString text;
  int next = 0;
  bool complete = false;
  bool valid = true;
};

PublicAgentJsonString decodePublicAgentJsonString(const QString &source, int start)
{
  PublicAgentJsonString result;
  result.next = start;
  if(start < 0 || start >= source.size() || source[start] != QLatin1Char('"'))
    return result;

  auto appendVisible = [&result](const QChar character) {
    const ushort code = character.unicode();
    if(code == ushort('\n') || code == ushort('\t') || code >= 0x20)
      result.text += character;
  };

  int index = start + 1;
  while(index < source.size())
  {
    const QChar character = source[index++];
    if(character == QLatin1Char('"'))
    {
      result.complete = true;
      result.next = index;
      return result;
    }
    if(character != QLatin1Char('\\'))
    {
      if(character.unicode() < 0x20)
      {
        result.valid = false;
        result.next = source.size();
        return result;
      }
      appendVisible(character);
      continue;
    }

    if(index >= source.size())
      break;
    const QChar escape = source[index++];
    if(escape == QLatin1Char('"') || escape == QLatin1Char('\\') ||
       escape == QLatin1Char('/'))
      appendVisible(escape);
    else if(escape == QLatin1Char('b'))
    {
      // Backspace has no useful transcript representation.
    }
    else if(escape == QLatin1Char('f'))
    {
      // Form feed has no useful transcript representation.
    }
    else if(escape == QLatin1Char('n'))
      appendVisible(QLatin1Char('\n'));
    else if(escape == QLatin1Char('r'))
      appendVisible(QLatin1Char('\n'));
    else if(escape == QLatin1Char('t'))
      appendVisible(QLatin1Char('\t'));
    else if(escape == QLatin1Char('u'))
    {
      if(index + 4 > source.size())
        break;
      ushort code = 0;
      bool valid = true;
      for(int digit = 0; digit < 4; ++digit)
      {
        const int value = publicAgentHexDigit(source[index + digit]);
        if(value < 0)
        {
          valid = false;
          break;
        }
        code = ushort((code << 4) | ushort(value));
      }
      if(!valid)
      {
        result.valid = false;
        result.next = source.size();
        return result;
      }
      index += 4;

      if(QChar::isHighSurrogate(code))
      {
        // Do not expose half of a surrogate pair while the next provider chunk is still pending.
        if(index + 6 > source.size())
          break;
        if(source[index] != QLatin1Char('\\') || source[index + 1] != QLatin1Char('u'))
        {
          result.valid = false;
          result.next = source.size();
          return result;
        }
        ushort low = 0;
        bool validLow = true;
        for(int digit = 0; digit < 4; ++digit)
        {
          const int value = publicAgentHexDigit(source[index + 2 + digit]);
          if(value < 0)
          {
            validLow = false;
            break;
          }
          low = ushort((low << 4) | ushort(value));
        }
        if(!validLow || !QChar::isLowSurrogate(low))
        {
          result.valid = false;
          result.next = source.size();
          return result;
        }
        result.text += QChar(code);
        result.text += QChar(low);
        index += 6;
      }
      else if(QChar::isLowSurrogate(code))
      {
        result.valid = false;
        result.next = source.size();
        return result;
      }
      else
      {
        appendVisible(QChar(code));
      }
    }
    else
    {
      result.valid = false;
      result.next = source.size();
      return result;
    }
  }
  result.next = source.size();
  return result;
}

class PublicAgentJsonNarrativeProjector
{
public:
  PublicAgentJsonNarrativeProjector(const QString &source, int start)
      : m_Source(source), m_Start(start)
  {
  }

  QString project()
  {
    parseValue(skipSpace(m_Start), 0);
    return m_Output;
  }

private:
  int skipSpace(int position) const
  {
    while(position < m_Source.size() && m_Source[position].isSpace())
      ++position;
    return position;
  }

  void appendPart(const QString &text)
  {
    if(text.isEmpty())
      return;
    if(!m_Output.isEmpty())
      m_Output += QLatin1Char('\n');
    m_Output += text;
  }

  int parseValue(int position, int depth)
  {
    position = skipSpace(position);
    if(position >= m_Source.size() || depth > 32)
      return m_Source.size();

    if(m_Source[position] == QLatin1Char('"'))
    {
      const PublicAgentJsonString value = decodePublicAgentJsonString(m_Source, position);
      if(value.valid && publicAgentNarrativePath(m_Path))
        appendPart(value.text);
      return value.next;
    }
    if(m_Source[position] == QLatin1Char('{'))
      return parseObject(position, depth + 1);
    if(m_Source[position] == QLatin1Char('['))
      return parseArray(position, depth + 1);

    while(position < m_Source.size() && m_Source[position] != QLatin1Char(',') &&
          m_Source[position] != QLatin1Char(']') && m_Source[position] != QLatin1Char('}'))
      ++position;
    return position;
  }

  int parseObject(int position, int depth)
  {
    ++position;
    while(position < m_Source.size())
    {
      position = skipSpace(position);
      if(position >= m_Source.size())
        return position;
      if(m_Source[position] == QLatin1Char('}'))
        return position + 1;
      if(m_Source[position] == QLatin1Char(','))
      {
        ++position;
        continue;
      }
      if(m_Source[position] != QLatin1Char('"'))
      {
        ++position;
        continue;
      }

      const PublicAgentJsonString key = decodePublicAgentJsonString(m_Source, position);
      if(!key.complete || !key.valid)
        return m_Source.size();
      position = skipSpace(key.next);
      if(position >= m_Source.size())
        return position;
      if(m_Source[position] != QLatin1Char(':'))
        continue;
      ++position;
      m_Path.push_back(key.text);
      position = parseValue(position, depth + 1);
      m_Path.removeLast();
    }
    return position;
  }

  int parseArray(int position, int depth)
  {
    ++position;
    while(position < m_Source.size())
    {
      position = skipSpace(position);
      if(position >= m_Source.size())
        return position;
      if(m_Source[position] == QLatin1Char(']'))
        return position + 1;
      if(m_Source[position] == QLatin1Char(','))
      {
        ++position;
        continue;
      }
      // i18n-ignore: Asterisk is an internal JSON-array path component.
      m_Path.push_back(lit("*"));
      position = parseValue(position, depth + 1);
      m_Path.removeLast();
    }
    return position;
  }

  const QString &m_Source;
  int m_Start = 0;
  QStringList m_Path;
  QString m_Output;
};

QString publicAgentMessageProjection(const QString &source, bool *structured,
                                     bool expectStructured = false)
{
  int start = 0;
  while(start < source.size() && (source[start].isSpace() || source[start] == QChar::ByteOrderMark))
    ++start;

  bool isStructured = false;
  const QString fence = QLatin1String("```json");
  const QString prefix = source.mid(start, qMin(fence.size(), source.size() - start));
  if(expectStructured)
  {
    // Provider observations can be contract objects.
    // Keep any prose/fence prefix private and begin exposing only schema-qualified string values
    // once the first object delimiter arrives.
    isStructured = true;
    const int objectStart = source.indexOf(QLatin1Char('{'), start);
    start = objectStart < 0 ? source.size() : objectStart;
  }
  else if(!prefix.isEmpty() && fence.startsWith(prefix, Qt::CaseInsensitive) &&
          prefix.size() < fence.size())
  {
    // Hold an undecided fenced-JSON prefix instead of briefly exposing backticks as prose.
    isStructured = true;
    start = source.size();
  }
  else if(source.midRef(start, fence.size()).compare(fence, Qt::CaseInsensitive) == 0)
  {
    isStructured = true;
    const int firstNewline = source.indexOf(QLatin1Char('\n'), start + fence.size());
    start = firstNewline < 0 ? source.size() : firstNewline + 1;
  }
  else if(start >= source.size())
  {
    // Leading whitespace is undecided until the first provider character arrives.
    isStructured = true;
  }
  else if(start < source.size() && source[start] == QLatin1Char('{'))
  {
    int next = start + 1;
    while(next < source.size() && source[next].isSpace())
      ++next;
    // A lone opening brace remains undecided across provider chunks. Once the following
    // character proves this is ordinary prose such as "{note}", preserve it unchanged.
    isStructured = next >= source.size() || source[next] == QLatin1Char('"') ||
                   source[next] == QLatin1Char('}');
  }
  else if(start < source.size() && source[start] == QLatin1Char('['))
  {
    int next = start + 1;
    while(next < source.size() && source[next].isSpace())
      ++next;
    isStructured = next >= source.size() || source[next] == QLatin1Char('{') ||
                   source[next] == QLatin1Char('[') || source[next] == QLatin1Char('"') ||
                   source[next] == QLatin1Char(']') || source[next] == QLatin1Char('-') ||
                   source[next].isDigit() || source[next] == QLatin1Char('t') ||
                   source[next] == QLatin1Char('f') || source[next] == QLatin1Char('n');
  }

  if(structured)
    *structured = isStructured;
  if(!isStructured)
    return source;

  // This tolerant parser deliberately accepts an unfinished JSON prefix. As soon as a narrative
  // string receives another complete escape/code point, the result grows by that exact suffix.
  // Protocol punctuation and non-narrative metadata never enter the visible target, and no final
  // QJsonDocument parse is needed before typewriter rendering can begin.
  return PublicAgentJsonNarrativeProjector(source, start).project();
}

struct AIProviderCatalogState
{
  QJsonArray presets;
};

struct AIHostCommandReply
{
  int exitCode = -1;
  QByteArray standardOutput;
  QString diagnostic;
  bool commandUnavailable = false;
};

typedef std::function<void(const AIHostCommandReply &)> AIHostCommandCompletion;
typedef std::function<void(const QStringList &arguments, const QByteArray &standardInput,
                           int timeoutMs, QObject *owner,
                           const AIHostCommandCompletion &completion)>
    AIHostCommandRunner;

// MSVC treats a direct QObject::connect template call inside a lambda declared in a QObject member
// as an implicit use of the member-function `this`, even when the static overload is qualified.
// Keep the asynchronous configuration runner genuinely QPointer-only by resolving the template in
// this free helper instead.
template <typename Sender, typename Signal, typename Context, typename Slot>
QMetaObject::Connection connectAIConfigurationSignal(Sender *sender, Signal signal,
                                                      Context *context, Slot &&slot)
{
  return QObject::connect(sender, signal, context, std::forward<Slot>(slot));
}

QString stableConfigurationId(const QString &displayName)
{
  // Settings asks for a name; the configuration files need an id. Deriving one keeps a
  // second field off the form without letting an invalid id reach the AgentHost.
  QString identifier;
  for(const QChar character : displayName.trimmed().toLower())
  {
    if(character.isLetterOrNumber() && character.unicode() < 128)
      identifier.append(character);
    else if(character == QLatin1Char('.') || character == QLatin1Char('-') ||
            character == QLatin1Char('_') || character.isSpace())
      identifier.append(QLatin1Char('-'));
  }
  while(identifier.contains(lit("--")))
    identifier.replace(lit("--"), lit("-"));    // i18n-ignore: identifier punctuation.
  while(identifier.startsWith(QLatin1Char('-')))
    identifier.remove(0, 1);
  while(identifier.endsWith(QLatin1Char('-')))
    identifier.chop(1);
  if(identifier.isEmpty())
  {
    // A name written entirely in a non-Latin script leaves nothing behind, and a
    // constant fallback made every such tool the same id -- so adding a second one
    // silently replaced the first. Derive the id from the name instead, so two
    // different names stay two different tools.
    identifier =
        lit("tool-") +    // i18n-ignore: fallback configuration identifier prefix.
        QString::fromLatin1(
            QCryptographicHash::hash(displayName.trimmed().toUtf8(), QCryptographicHash::Sha256)
                .toHex()
                .left(12));    // i18n-ignore: fallback configuration identifier.
  }
  return identifier.left(64);
}

bool isLowerHexIdentifier(const QString &value, int expectedLength)
{
  if(value.size() != expectedLength)
    return false;
  for(const QChar character : value)
  {
    const ushort code = character.unicode();
    if(!((code >= ushort('0') && code <= ushort('9')) ||
         (code >= ushort('a') && code <= ushort('f'))))
      return false;
  }
  return true;
}

bool decodeContextSnapshot(const QByteArray &encoded, QJsonObject *snapshot = NULL)
{
  QJsonParseError parseError;
  const QJsonDocument document = QJsonDocument::fromJson(encoded, &parseError);
  const QJsonObject object = document.isObject() ? document.object() : QJsonObject();
  // i18n-ignore: ReplayLab frozen-context protocol field names and schema identifiers.
  const QJsonObject capture = object.value(lit("capture")).toObject();
  const bool valid = parseError.error == QJsonParseError::NoError &&
                     encoded.size() > 0 && encoded.size() <= MaxContextSnapshotBytes &&
                     object.value(lit("schema")).toString() ==    // i18n-ignore: frozen-context protocol key
                         lit("renderdoc-ultra-current-context/v1") &&    // i18n-ignore: frozen-context schema ID
                     isLowerHexIdentifier(object.value(lit("snapshotId")).toString(), 64) &&    // i18n-ignore: frozen-context protocol key
                     capture.value(lit("loaded")).toBool() &&    // i18n-ignore: frozen-context protocol key
                     isLowerHexIdentifier(
                         capture.value(lit("captureSessionId")).toString(), 32);    // i18n-ignore: frozen-context protocol key
  if(valid && snapshot)
    *snapshot = object;
  return valid;
}

struct ChatContextFreezeEnvelope
{
  QString jobId;
  QString status;
  QString captureSessionId;
  QString resultPath;
  QString diagnostic;
  qint64 currentEventId = 0;
  qint64 selectedEventId = 0;
  QByteArray contextSnapshot;
};

bool decodeChatContextFreezeEnvelope(const QByteArray &encoded, bool terminalOnly,
                                     ChatContextFreezeEnvelope &result)
{
  result = ChatContextFreezeEnvelope();
  if(encoded.isEmpty() || encoded.size() > MaxContextFreezeResultBytes)
    return false;

  QJsonParseError parseError;
  const QJsonDocument document = QJsonDocument::fromJson(encoded, &parseError);
  if(parseError.error != QJsonParseError::NoError || !document.isObject())
    return false;
  const QJsonObject object = document.object();
  // i18n-ignore: ReplayLab asynchronous context-freeze protocol fields and schema ID.
  if(object.value(lit("schema")).toString() !=
         // i18n-ignore: ReplayLab context-freeze protocol schema identifier.
         lit("replaylab-current-context-freeze-job/v1") ||
     // i18n-ignore: ReplayLab context-freeze protocol version field.
     object.value(lit("schemaVersion")).toInt() != 1)
    return false;

  // i18n-ignore: ReplayLab asynchronous context-freeze protocol fields.
  result.jobId = object.value(lit("jobId")).toString();
  // i18n-ignore: ReplayLab context-freeze protocol status field.
  result.status = object.value(lit("status")).toString();
  // i18n-ignore: ReplayLab context-freeze protocol capture identifier field.
  result.captureSessionId = object.value(lit("captureSessionId")).toString();
  // i18n-ignore: ReplayLab context-freeze protocol result path field.
  result.resultPath = object.value(lit("resultPath")).toString();
  // i18n-ignore: ReplayLab context-freeze protocol diagnostic field.
  result.diagnostic = object.value(lit("error")).toString().left(4096);
  bool currentEventOK = false;
  bool selectedEventOK = false;
  // i18n-ignore: ReplayLab asynchronous context-freeze protocol fields.
  result.currentEventId =
      // i18n-ignore: ReplayLab context-freeze protocol current-event field.
      object.value(lit("currentEventId")).toVariant().toLongLong(&currentEventOK);
  result.selectedEventId =
      // i18n-ignore: ReplayLab context-freeze protocol selected-event field.
      object.value(lit("selectedEventId")).toVariant().toLongLong(&selectedEventOK);

  // i18n-ignore: ReplayLab asynchronous context-freeze lifecycle states.
  const bool terminal = result.status == lit("completed") || result.status == lit("failed") ||
                        // i18n-ignore: ReplayLab context-freeze lifecycle state.
                        result.status == lit("abandoned");
  // i18n-ignore: ReplayLab context-freeze lifecycle state.
  const bool validStatus = terminal || result.status == lit("queued") ||
                           // i18n-ignore: ReplayLab context-freeze lifecycle state.
                           result.status == lit("running");
  if(!isLowerHexIdentifier(result.jobId, 32) ||
     !isLowerHexIdentifier(result.captureSessionId, 32) || result.resultPath.isEmpty() ||
     result.resultPath.size() > 32768 || !currentEventOK || !selectedEventOK ||
     result.currentEventId < 0 || result.currentEventId > 0xFFFFFFFFLL ||
     result.selectedEventId < 0 || result.selectedEventId > 0xFFFFFFFFLL || !validStatus ||
     (terminalOnly && !terminal))
    return false;

  if(result.status == lit("completed"))    // i18n-ignore: context-freeze lifecycle state
  {
    // i18n-ignore: ReplayLab asynchronous context-freeze protocol field.
    const QJsonObject snapshot = object.value(lit("context")).toObject();
    result.contextSnapshot = QJsonDocument(snapshot).toJson(QJsonDocument::Compact);
    QJsonObject decodedSnapshot;
    if(snapshot.isEmpty() || !decodeContextSnapshot(result.contextSnapshot, &decodedSnapshot))
      return false;
    // i18n-ignore: Frozen-context capture and event identity fields.
    const QJsonObject capture = decodedSnapshot.value(lit("capture")).toObject();
    // i18n-ignore: Frozen-context event object field.
    const QJsonObject events = decodedSnapshot.value(lit("events")).toObject();
    // i18n-ignore: Frozen-context capture-session identifier field.
    if(capture.value(lit("captureSessionId")).toString() != result.captureSessionId ||
       // i18n-ignore: Frozen-context current-event identifier field.
       events.value(lit("currentEventId")).toVariant().toLongLong() != result.currentEventId ||
       // i18n-ignore: Frozen-context selected-event identifier field.
       events.value(lit("selectedEventId")).toVariant().toLongLong() != result.selectedEventId)
      return false;
  }
  return true;
}

QJsonObject contextCandidateJSON(const QByteArray &encoded, const QString &instanceId)
{
  QJsonObject snapshot;
  if(instanceId.trimmed().isEmpty() || instanceId.size() > 160 ||
     !decodeContextSnapshot(encoded, &snapshot))
    return QJsonObject();

  QJsonObject candidate;
  // i18n-ignore: ReplayLab clarification context-candidate wire-protocol fields.
  candidate[lit("contextInstanceId")] = instanceId;
  candidate[lit("snapshot")] = snapshot;    // i18n-ignore: context-candidate protocol key
  return candidate;
}

bool normalizedTerminalStatus(const QJsonObject &object, QString &status)
{
  // i18n-ignore: ReplayLab result/task-event JSON field name defined by the backend protocol.
  if(!object.contains(lit("terminalStatus")))
  {
    // i18n-ignore: Task lifecycle state identifier used only for control flow.
    status = lit("completed");
    return true;
  }

  // i18n-ignore: ReplayLab result/task-event JSON field name defined by the backend protocol.
  const QJsonValue value = object.value(lit("terminalStatus"));
  if(!value.isString())
    return false;
  status = value.toString();
  // i18n-ignore: Task lifecycle state identifiers used only for control flow.
  return status == lit("completed") || status == lit("partial") ||
         // i18n-ignore: Task lifecycle state identifier used only for control flow.
         status == lit("needs-user-input") ||
         // i18n-ignore: Task lifecycle state identifier used only for control flow.
         status == lit("blocked");
}

QString htmlText(const QString &text)
{
  QString escaped = text.toHtmlEscaped();
  escaped.replace(QLatin1Char('\n'), lit("<br>"));
  return escaped;
}

enum class AssistantActionIcon
{
  Add,
  Brush,
  Send,
  Stop,
};

QIcon assistantActionIcon(const QWidget *widget, AssistantActionIcon kind)
{
  QPixmap pixmap(18, 18);
  pixmap.fill(Qt::transparent);
  QPainter painter(&pixmap);
  painter.setRenderHint(QPainter::Antialiasing, true);
  const QColor color = widget ? widget->palette().color(QPalette::ButtonText)
                              : QApplication::palette().color(QPalette::ButtonText);
  painter.setPen(QPen(color, 1.7, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
  painter.setBrush(Qt::NoBrush);
  if(kind == AssistantActionIcon::Add)
  {
    painter.drawLine(QPointF(4.5, 9.0), QPointF(13.5, 9.0));
    painter.drawLine(QPointF(9.0, 4.5), QPointF(9.0, 13.5));
  }
  else if(kind == AssistantActionIcon::Brush)
  {
    painter.drawLine(QPointF(7, 11), QPointF(14, 3));
    painter.drawLine(QPointF(9, 12), QPointF(15, 4));
    QPainterPath tip;
    tip.moveTo(8, 10);
    tip.cubicTo(3, 8, 6, 14, 2, 15);
    tip.cubicTo(7, 16, 11, 14, 8, 10);
    painter.drawPath(tip);
  }
  else if(kind == AssistantActionIcon::Send)
  {
    QPainterPath arrow;
    arrow.moveTo(4.5, 9.5);
    arrow.lineTo(9.0, 5.0);
    arrow.lineTo(13.5, 9.5);
    painter.drawPath(arrow);
    painter.drawLine(QPointF(9.0, 5.5), QPointF(9.0, 13.5));
  }
  else if(kind == AssistantActionIcon::Stop)
  {
    painter.setPen(Qt::NoPen);
    painter.setBrush(color);
    painter.drawRoundedRect(QRectF(5.0, 5.0, 8.0, 8.0), 1.5, 1.5);
  }
  return QIcon(pixmap);
}

enum class DiagnosticLevel
{
  Info = 0,
  Success = 1,
  Warning = 2,
  Error = 3,
};

DiagnosticLevel diagnosticLevel(const QString &status)
{
  const QString value = status.trimmed().toLower();
  if(value == lit("ok") || value == lit("success") || value == lit("passed") ||
     value == lit("connected") || value == lit("authenticated"))
    return DiagnosticLevel::Success;
  if(value == lit("warning") || value == lit("warn") || value == lit("skipped") ||
     value == lit("skip") || value == lit("unknown") || value == lit("unavailable"))
    return DiagnosticLevel::Warning;
  if(value == lit("error") || value == lit("failed") || value == lit("failure"))
    return DiagnosticLevel::Error;
  return DiagnosticLevel::Info;
}

QString diagnosticTag(DiagnosticLevel level, const QString &rawStatus = QString())
{
  const QString normalized = rawStatus.trimmed().toLower();
  if(normalized == lit("skip") || normalized == lit("skipped"))
    return QCoreApplication::translate("AIAssistant", "SKIPPED");
  switch(level)
  {
    case DiagnosticLevel::Success:
      return QCoreApplication::translate("AIAssistant", "OK");
    case DiagnosticLevel::Warning:
      return QCoreApplication::translate("AIAssistant", "WARNING");
    case DiagnosticLevel::Error:
      return QCoreApplication::translate("AIAssistant", "ERROR");
    default: return QCoreApplication::translate("AIAssistant", "INFO");
  }
}

QColor diagnosticColor(const QWidget *widget, DiagnosticLevel level)
{
  const QPalette palette = widget ? widget->palette() : QApplication::palette();
  const QColor background = palette.color(QPalette::Base);
  const bool dark = background.lightness() < 128;
  switch(level)
  {
    case DiagnosticLevel::Success: return QColor(dark ? lit("#69DBA5") : lit("#147A50"));
    case DiagnosticLevel::Warning: return QColor(dark ? lit("#FFD166") : lit("#8A5800"));
    case DiagnosticLevel::Error: return QColor(dark ? lit("#FF7B7B") : lit("#B3261E"));
    default: return palette.color(QPalette::Text);
  }
}

void applyDiagnosticPalette(QWidget *widget, DiagnosticLevel level)
{
  if(!widget)
    return;
  QPalette palette = widget->palette();
  const QColor color = diagnosticColor(widget, level);
  palette.setColor(QPalette::WindowText, color);
  palette.setColor(QPalette::Text, color);
  widget->setPalette(palette);
}

QString diagnosticStatusText(DiagnosticLevel level, const QString &message,
                             const QString &rawStatus = QString())
{
  return lit("[") + diagnosticTag(level, rawStatus) + lit("] ") + message;
}

void setDiagnosticStatus(QLabel *label, DiagnosticLevel level, const QString &message,
                         const QString &rawStatus = QString())
{
  if(!label)
    return;
  label->setTextFormat(Qt::PlainText);
  applyDiagnosticPalette(label, level);
  const QString text = diagnosticStatusText(level, message, rawStatus);
  label->setText(text);
  label->setAccessibleName(text);
}

void setDiagnosticStatus(QTextBrowser *browser, DiagnosticLevel level, const QString &message,
                         const QString &rawStatus = QString())
{
  if(!browser)
    return;
  applyDiagnosticPalette(browser, level);
  const QString text = diagnosticStatusText(level, message, rawStatus);
  browser->setPlainText(text);
  browser->setAccessibleName(text);
}

bool safeProviderAuthUrl(const QUrl &url)
{
  const QString scheme = url.scheme().toLower();
  const QString host = url.host().toLower();
  if(!url.isValid() || host.isEmpty())
    return false;
  return scheme == lit("https") ||
         (scheme == lit("http") &&
          (host == lit("127.0.0.1") || host == lit("localhost") || host == lit("::1")));
}

struct SubscriptionProcessState
{
  QPointer<QProcess> process;
  QPointer<QTimer> cancelTimer;
  QMetaObject::Connection cancelConnection;
  QByteArray tail;
  qint64 outputBytes = 0;
  QStringList discoveredModels;
  QString operation;
  QString presetId;
  QString failureDetail;
  void *job = NULL;
  void *processInformation = NULL;
  bool agentProviderEnabled = false;
  bool agentCommandEnabled = false;
  bool trustACPEnabled = false;
  bool authFlowEnabled = false;
  bool modelsBoxEnabled = false;
  bool saveButtonEnabled = false;
  bool completionHandled = false;
  bool testTerminalSucceeded = false;
  bool operationTerminalSucceeded = false;

  ~SubscriptionProcessState()
  {
    // Dialog teardown normally closes this explicitly. Retain a final kill-on-close owner so an
    // exceptional QObject teardown cannot leak the HANDLE or leave a provider child running.
#if defined(Q_OS_WIN)
    if(job)
      CloseHandle(static_cast<HANDLE>(job));
#endif
    job = NULL;
  }
};

void setAuthLinkStatus(QTextBrowser *browser, DiagnosticLevel level, const QString &message,
                       const QUrl &authUrl, const QString &deviceCode)
{
  if(!browser)
    return;
  applyDiagnosticPalette(browser, level);
  const QString tag = diagnosticTag(level).toHtmlEscaped();
  const QString color = diagnosticColor(browser, level).name(QColor::HexRgb).toHtmlEscaped();
  QString contents = lit("<div><b><span style=\"color:%1\">[%2]</span></b> %3")
                         .arg(color, tag, htmlText(message));
  if(!deviceCode.isEmpty())
    contents += QCoreApplication::translate("AIAssistant", "<br>Device code: <code>%1</code>")
                    .arg(deviceCode.toHtmlEscaped());
  if(!authUrl.isEmpty())
  {
    const QString encoded = authUrl.toString(QUrl::FullyEncoded);
    contents += QCoreApplication::translate(
                    "AIAssistant", "<br>Authorization URL: <a href=\"%1\">%2</a>")
                    .arg(encoded.toHtmlEscaped(), encoded.toHtmlEscaped());
  }
  contents += lit("</div>");
  browser->setHtml(contents);
  browser->setAccessibleName(
      diagnosticStatusText(level, message) +
      (deviceCode.isEmpty()
           ? QString()
           : QCoreApplication::translate("AIAssistant", " Device code: %1").arg(deviceCode)) +
      (authUrl.isEmpty()
           ? QString()
           : QCoreApplication::translate("AIAssistant", " Authorization URL: %1")
                 .arg(authUrl.toString(QUrl::FullyEncoded))));
}

QIcon diagnosticIcon(QWidget *widget, DiagnosticLevel level)
{
  QStyle *style = widget ? widget->style() : QApplication::style();
  switch(level)
  {
    case DiagnosticLevel::Success: return style->standardIcon(QStyle::SP_DialogApplyButton);
    case DiagnosticLevel::Warning: return style->standardIcon(QStyle::SP_MessageBoxWarning);
    case DiagnosticLevel::Error: return style->standardIcon(QStyle::SP_MessageBoxCritical);
    default: return style->standardIcon(QStyle::SP_MessageBoxInformation);
  }
}

QString localizeProviderRuntimeMessage(const QString &presetId, const QString &messageId,
                                       const QJsonArray &arguments, const QString &fallback);

void showConnectionDiagnostics(QWidget *parent, const QString &title, const QJsonDocument &report,
                               int exitCode, const QString &processDetail)
{
  QDialog dialog(parent);
  dialog.setWindowTitle(title);
  dialog.resize(820, 520);
  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QLabel *summary = new QLabel(&dialog);
  summary->setWordWrap(true);
  layout->addWidget(summary);

  QTreeWidget *checks = new QTreeWidget(&dialog);
  checks->setColumnCount(3);
  checks->setHeaderLabels(QStringList() << QCoreApplication::translate("AIAssistant", "Status")
                                        << QCoreApplication::translate("AIAssistant", "Check")
                                        << QCoreApplication::translate("AIAssistant", "Detail"));
  checks->setRootIsDecorated(false);
  checks->setAlternatingRowColors(true);
  checks->setSelectionMode(QAbstractItemView::SingleSelection);
  checks->header()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
  checks->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
  checks->header()->setSectionResizeMode(2, QHeaderView::Stretch);
  layout->addWidget(checks, 1);

  QLabel *detailTitle = new QLabel(QCoreApplication::translate(
      "AIAssistant", "Selected detail (plain text; select or copy as needed):"), &dialog);
  layout->addWidget(detailTitle);
  QTextBrowser *detail = new QTextBrowser(&dialog);
  detail->setOpenExternalLinks(false);
  detail->setMaximumHeight(120);
  layout->addWidget(detail);

  QStringList copyLines;
  DiagnosticLevel overall = DiagnosticLevel::Info;
  bool hasRows = false;
  auto addRow = [&](DiagnosticLevel level, const QString &rawStatus, const QString &name,
                    const QString &message) {
    hasRows = true;
    if(int(level) > int(overall))
      overall = level;
    const QString tag = diagnosticTag(level, rawStatus);
    QTreeWidgetItem *item = new QTreeWidgetItem(checks);
    item->setText(0, tag);
    item->setText(1, name);
    item->setText(2, message);
    item->setIcon(0, diagnosticIcon(checks, level));
    const QBrush brush(diagnosticColor(checks, level));
    item->setForeground(0, brush);
    item->setForeground(1, brush);
    item->setToolTip(2, message);
    item->setData(2, Qt::UserRole, message);
    copyLines << lit("[") + tag + lit("] ") + name + lit(" - ") + message;
  };

  if(report.isArray())
  {
    for(const QJsonValue &entry : report.array())
    {
      if(!entry.isObject())
      {
        addRow(DiagnosticLevel::Warning, lit("warning"),
               QCoreApplication::translate("AIAssistant", "Diagnostic record"),
               QCoreApplication::translate("AIAssistant", "The host returned a non-object row."));
        continue;
      }
      const QJsonObject item = entry.toObject();
      const QString rawStatus = item.value(lit("status")).toString();
      // i18n-ignore: Host diagnostic JSON field name.
      QString name = item.value(lit("name")).toString(
          QCoreApplication::translate("AIAssistant", "Unnamed check"));
      // i18n-ignore: stable Host diagnostic check identifier.
      if(name == QLatin1String("claude-authentication"))
        name = QCoreApplication::translate("AIAssistant", "Claude sign-in");
      // i18n-ignore: stable Host diagnostic check identifiers.
      if(name == QLatin1String("portable-capabilities"))
        name = QCoreApplication::translate("AIAssistant", "Bundled tool dependencies");
      // i18n-ignore: stable Host diagnostic check identifier.
      if(name == QLatin1String("portable-tool-execution"))
        name = QCoreApplication::translate("AIAssistant", "Document tool execution");
      // i18n-ignore: stable Host diagnostic check identifier.
      if(name == QLatin1String("model-tool-execution"))
        name = QCoreApplication::translate("AIAssistant", "Model and MCP execution");
      // i18n-ignore: stable Host diagnostic check identifier.
      if(name == QLatin1String("bread-host-execution"))
        name = QCoreApplication::translate("AIAssistant", "Bread Host execution");
      // i18n-ignore: Host diagnostic JSON field name.
      const QString fallback = item.value(lit("detail")).toString(
          QCoreApplication::translate("AIAssistant", "No detail was returned."));
      // i18n-ignore: stable Host diagnostic message ID and positional arguments.
      const QString message = localizeProviderRuntimeMessage(
          // i18n-ignore: Host diagnostic JSON message ID field.
          QString(), item.value(lit("messageId")).toString(),
          // i18n-ignore: Host diagnostic JSON positional arguments field.
          item.value(lit("messageArgs")).toArray(), fallback);
      addRow(diagnosticLevel(rawStatus), rawStatus,
             name, message);
    }
  }
  else
  {
    addRow(DiagnosticLevel::Error, lit("error"),
           QCoreApplication::translate("AIAssistant", "Diagnostic output"),
           processDetail.isEmpty()
               ? QCoreApplication::translate("AIAssistant",
                                             "The host returned invalid diagnostic data.")
               : processDetail);
  }

  if(!processDetail.isEmpty() && report.isArray())
    addRow(exitCode == 0 ? DiagnosticLevel::Warning : DiagnosticLevel::Error,
           exitCode == 0 ? lit("warning") : lit("error"),
           QCoreApplication::translate("AIAssistant", "Process detail"), processDetail);
  if(exitCode != 0 && overall != DiagnosticLevel::Error)
    addRow(DiagnosticLevel::Error, lit("error"),
           QCoreApplication::translate("AIAssistant", "Process result"),
           QCoreApplication::translate("AIAssistant",
                                       "The diagnostic command did not complete successfully."));
  if(!hasRows)
    addRow(DiagnosticLevel::Warning, lit("warning"),
           QCoreApplication::translate("AIAssistant", "Connection check"),
           QCoreApplication::translate("AIAssistant", "No diagnostic details were returned."));

  QString summaryMessage;
  if(overall == DiagnosticLevel::Error)
    summaryMessage = QCoreApplication::translate("AIAssistant",
                                                 "One or more checks failed. Review the details.");
  else if(overall == DiagnosticLevel::Warning)
    summaryMessage = QCoreApplication::translate(
        "AIAssistant", "Checks completed with warnings or skipped items.");
  else if(overall == DiagnosticLevel::Success)
    summaryMessage = QCoreApplication::translate("AIAssistant", "All reported checks passed.");
  else
    summaryMessage = QCoreApplication::translate("AIAssistant",
                                                 "Diagnostic information was returned.");
  setDiagnosticStatus(summary, overall, summaryMessage);
  dialog.setWindowIcon(diagnosticIcon(&dialog, overall));

  QObject::connect(checks, &QTreeWidget::currentItemChanged, &dialog,
                   [detail](QTreeWidgetItem *current, QTreeWidgetItem *) {
                     detail->setPlainText(current ? current->data(2, Qt::UserRole).toString()
                                                  : QString());
                   });
  if(checks->topLevelItemCount() > 0)
    checks->setCurrentItem(checks->topLevelItem(0));

  QDialogButtonBox *buttons = new QDialogButtonBox(QDialogButtonBox::Close, &dialog);
  QPushButton *copy = buttons->addButton(
      QCoreApplication::translate("AIAssistant", "Copy All Details"),
      QDialogButtonBox::ActionRole);
  QObject::connect(copy, &QPushButton::clicked, &dialog,
                   [copyLines]() { QApplication::clipboard()->setText(copyLines.join(lit("\n"))); });
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  layout->addWidget(buttons);
  dialog.exec();
}

struct LiveConnectionDiagnostics
{
  QPointer<QDialog> dialog;
  QLabel *summary = NULL;
  QTreeWidget *checks = NULL;
  QTextBrowser *detail = NULL;
  QMap<int, QTreeWidgetItem *> rows;
  QByteArray pending;
  bool receivedComplete = false;

  void row(int index, const QJsonObject &record)
  {
    if(!dialog || index < 0 || index > 1024)
      return;
    const QString status = record.value(lit("status")).toString();  // i18n-ignore: doctor JSON field.
    QString name = record.value(lit("name")).toString();  // i18n-ignore: doctor JSON field.
    // i18n-ignore: stable backend check identifiers.
    if(name == lit("portable-tool-execution"))
      name = QCoreApplication::translate("AIAssistant", "Document tool execution");
    else if(name == lit("bread-host-execution"))  // i18n-ignore: doctor check identifier.
      name = QCoreApplication::translate("AIAssistant", "Bread Host execution");
    else if(name == lit("model-tool-execution"))  // i18n-ignore: doctor check identifier.
      name = QCoreApplication::translate("AIAssistant", "Model and MCP execution");
    else if(name == lit("portable-capabilities"))  // i18n-ignore: doctor check identifier.
      name = QCoreApplication::translate("AIAssistant", "Bundled tool dependencies");
    else if(name == lit("claude-authentication"))  // i18n-ignore: doctor check identifier.
      name = QCoreApplication::translate("AIAssistant", "Claude sign-in");
    const QString message = localizeProviderRuntimeMessage(QString(),
        record.value(lit("messageId")).toString(), record.value(lit("messageArgs")).toArray(),  // i18n-ignore: doctor JSON fields.
        record.value(lit("detail")).toString());  // i18n-ignore: doctor JSON field.
    QTreeWidgetItem *item = rows.value(index, NULL);
    if(!item)
    {
      item = new QTreeWidgetItem(checks);
      rows.insert(index, item);
    }
    const DiagnosticLevel level = diagnosticLevel(status);
    // i18n-ignore: stable backend running status.
    item->setText(0, status == lit("running") ? QCoreApplication::translate("AIAssistant", "Checking...") : diagnosticTag(level, status));
    item->setText(1, name);
    item->setText(2, message);
    item->setData(0, Qt::UserRole, status);
    item->setData(2, Qt::UserRole, message);
    item->setToolTip(2, message);
    item->setIcon(0, diagnosticIcon(checks, level));
    item->setForeground(0, QBrush(diagnosticColor(checks, level)));
    if(checks->currentItem() == item)
      detail->setPlainText(message);
  }

  void consume(const QByteArray &chunk)
  {
    if(!dialog)
      return;
    pending += chunk;
    if(pending.size() > 1024 * 1024)
    {
      pending.clear();
      return;
    }
    int newline = -1;
    while((newline = pending.indexOf('\n')) >= 0)
    {
      const QJsonObject event = QJsonDocument::fromJson(pending.left(newline)).object();
      pending.remove(0, newline + 1);
      // i18n-ignore: incremental diagnostic protocol field names and values.
      if(event.value(lit("type")).toString() == lit("check"))
        row(event.value(lit("index")).toInt(-1), event.value(lit("check")).toObject());  // i18n-ignore: doctor JSON fields.
      else if(event.value(lit("type")).toString() == lit("complete"))  // i18n-ignore: doctor JSON event type.
        receivedComplete = true;
    }
  }
};

bool safeTaskId(const QString &taskId)
{
  static const QRegularExpression valid(lit("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"));
  return valid.match(taskId).hasMatch();
}

QString conversationCleanupKey(const QString &conversationId)
{
  return QString::fromLatin1(
             QCryptographicHash::hash(conversationId.toUtf8(), QCryptographicHash::Sha256).toHex()) +
         QStringLiteral(".json");
}

bool saveConversationCleanupRequest(const QString &eventRoot, const QString &conversationId,
                                    const QSet<QString> &taskIds)
{
  const QString directory = QDir(eventRoot).filePath(QStringLiteral(".cleanup-pending"));
  for(QString parent = directory; !parent.isEmpty();)
  {
    const QFileInfo info(parent);
    if(info.exists() && (info.isSymLink() ||
       QDir::cleanPath(info.canonicalFilePath()).compare(
           QDir::cleanPath(info.absoluteFilePath()), Qt::CaseInsensitive) != 0))
      return false;
    const QString next = info.absolutePath();
    if(next == parent)
      break;
    parent = next;
  }
  if(!QDir().mkpath(directory))
    return false;
  const QFileInfo directoryInfo(directory);
  if(directoryInfo.isSymLink() ||
     QDir::cleanPath(directoryInfo.canonicalFilePath()).compare(
         QDir::cleanPath(directoryInfo.absoluteFilePath()), Qt::CaseInsensitive) != 0)
    return false;
  const QString path = QDir(directory).filePath(conversationCleanupKey(conversationId));
  if(QFileInfo(path).isSymLink())
    return false;
  QJsonArray tasks;
  for(const QString &task : taskIds)
    tasks.push_back(task);
  // i18n-ignore: Durable deletion request schema/fields, no conversation contents.
  const QJsonObject request{{QStringLiteral("schema"), QStringLiteral("bread-conversation-cleanup/v1")},
                            {QStringLiteral("conversationId"), conversationId},
                            {QStringLiteral("taskIds"), tasks}};
  const QByteArray payload = QJsonDocument(request).toJson(QJsonDocument::Compact);
  QSaveFile file(path);
  file.setDirectWriteFallback(false);
  return file.open(QIODevice::WriteOnly) && file.write(payload) == payload.size() && file.commit();
}

bool conversationDeletionRequested(const QString &eventRoot, const QString &conversationId)
{
  const QString key = conversationCleanupKey(conversationId);
  for(const QString &folder : {QStringLiteral(".cleanup-pending"),
                               QStringLiteral(".deleted-conversations")})
  {
    QFile file(QDir(QDir(eventRoot).filePath(folder)).filePath(key));
    if(QFileInfo(file).isSymLink() || !file.open(QIODevice::ReadOnly) || file.size() > 2 * 1024 * 1024)
      continue;
    const QJsonObject request = QJsonDocument::fromJson(file.readAll()).object();
    // i18n-ignore: Durable deletion request schema and conversation identity fields.
    if(request.value(QStringLiteral("schema")).toString() == QStringLiteral("bread-conversation-cleanup/v1") &&
       request.value(QStringLiteral("conversationId")).toString() == conversationId)
      return true;
  }
  return false;
}

bool validAttachmentKind(const QString &kind)
{
  // i18n-ignore: Attachment kinds are stable ReplayLab wire-protocol identifiers.
  return kind == lit("text") || kind == lit("code") || kind == lit("text-file") ||
         // i18n-ignore: Attachment kinds are stable ReplayLab wire-protocol identifiers.
         kind == lit("image") || kind == lit("file") || kind == lit("file-ref");
}

bool validAttachmentMediaType(const QString &mediaType)
{
  if(mediaType.isEmpty() || mediaType.size() > 160 || mediaType != mediaType.trimmed() ||
     mediaType.contains(QChar::Null) || mediaType.contains(QLatin1Char('\r')) ||
     mediaType.contains(QLatin1Char('\n')))
    return false;
  const QString normalized = mediaType.section(QLatin1Char(';'), 0, 0).trimmed().toLower();
  // i18n-ignore: This regular expression validates a non-user-facing MIME type/subtype token.
  static const QRegularExpression validEssence(
      lit("^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"));    // i18n-ignore: MIME token syntax
  return validEssence.match(normalized).hasMatch();
}

QString attachmentMediaTypeEssence(const QString &mediaType)
{
  return mediaType.section(QLatin1Char(';'), 0, 0).trimmed().toLower();
}

bool validTextAttachmentMediaType(const QString &mediaType)
{
  if(!validAttachmentMediaType(mediaType))
    return false;
  const QString normalized = attachmentMediaTypeEssence(mediaType);
  if(normalized.startsWith(QLatin1String("text/")) ||
     (normalized.startsWith(QLatin1String("application/")) &&
      (normalized.endsWith(QLatin1String("+json")) ||
       normalized.endsWith(QLatin1String("+xml")))))
    return true;
  // i18n-ignore: MIME types are protocol identifiers for validated UTF-8 textual content.
  static const QSet<QString> textualApplicationTypes = {
      lit("application/json"),         lit("application/xml"),    // i18n-ignore: MIME IDs
      lit("application/yaml"),         lit("application/x-yaml"),    // i18n-ignore: MIME IDs
      lit("application/toml"),         lit("application/x-toml"),    // i18n-ignore: MIME IDs
      lit("application/javascript"),   lit("application/x-javascript"),    // i18n-ignore: MIME IDs
      lit("application/sql"),          lit("application/graphql"),    // i18n-ignore: MIME IDs
  };
  return textualApplicationTypes.contains(normalized);
}

bool isTextAttachment(const AIChatAttachment &attachment)
{
  // i18n-ignore: Attachment kinds are stable ReplayLab wire-protocol identifiers.
  return attachment.kind == lit("text") || attachment.kind == lit("code") ||
         // i18n-ignore: Attachment kinds are stable ReplayLab wire-protocol identifiers.
         attachment.kind == lit("text-file");
}

bool isBinaryAttachment(const AIChatAttachment &attachment)
{
  // i18n-ignore: Attachment kinds are stable ReplayLab wire-protocol identifiers.
  return attachment.kind == lit("image") || attachment.kind == lit("file");
}

bool isPathReferenceAttachment(const AIChatAttachment &attachment)
{
  // Keep the provisional file-reference wire contract in one helper so it can be changed without
  // searching UI and persistence code when AgentHost finalises the schema.
  return attachment.kind == QLatin1String("file-ref") &&
         attachment.encoding == QLatin1String("path");
}

bool attachmentRequiresV4(const AIChatAttachment &attachment)
{
  return isBinaryAttachment(attachment);
}

bool validAttachmentContext(const QJsonObject &context)
{
  if(context.isEmpty())
    return true;
  // i18n-ignore: Attachment context schema and object names are stable wire-protocol values.
  if(context.value(lit("schema")).toString() !=
         QLatin1String("replaylab-attachment-context/v1") ||
     QJsonDocument(context).toJson(QJsonDocument::Compact).size() >
         MaxAttachmentContextBytes)
    return false;
  static const char *const objectFields[] = {"origin", "capture", "shader", "resource"};
  for(const char *field : objectFields)
  {
    const QJsonValue value = context.value(QString::fromLatin1(field));
    if(!value.isUndefined() && !value.isObject())
      return false;
  }
  const QJsonObject origin =
      context.value(lit("origin")).toObject();    // i18n-ignore: attachment protocol field
  const QString panel =
      origin.value(lit("panel")).toString();    // i18n-ignore: attachment protocol field
  static const QRegularExpression validPanel(
      lit("^[a-z][a-z0-9-]{0,63}$"));    // i18n-ignore: attachment protocol ID syntax
  if(panel.isEmpty() || !validPanel.match(panel).hasMatch())
    return false;
  // ResourceId is uint64 and cannot survive Qt's JSON double conversion. Reject numeric IDs
  // before they enter conversation history or launch a provider (the Python schema is strict).
  for(const char *field : {"shader", "resource"})
  {
    const QJsonValue resourceId = context.value(QLatin1String(field)).toObject().value(
        QLatin1String("resourceId"));
    if(!resourceId.isUndefined() && (!resourceId.isString() || resourceId.toString().isEmpty()))
      return false;
  }
  const QJsonValue relations =
      context.value(lit("relations"));    // i18n-ignore: attachment protocol field
  return relations.isUndefined() || relations.isArray();
}

bool validPathReference(const AIChatAttachment &attachment)
{
  if(!isPathReferenceAttachment(attachment) || attachment.content.isEmpty() ||
     attachment.content.size() > MaxAttachmentPathChars ||
     attachment.content.contains(QChar::Null) ||
     attachment.content.contains(QLatin1Char('\r')) ||
     attachment.content.contains(QLatin1Char('\n')) ||
     attachment.originalBytes <= 0 || attachment.originalBytes > MaxPathReferenceBytes ||
     attachment.originalBytes > MaxSafeJSONInteger || attachment.modifiedUnixMs <= 0 ||
     attachment.modifiedUnixMs > MaxSafeJSONInteger ||
     attachment.source != QLatin1String("file-picker"))
    return false;
  const QFileInfo pathInfo(attachment.content);
  return pathInfo.isAbsolute();
}

bool attachmentRequiresV5(const AIChatAttachment &attachment)
{
  return isPathReferenceAttachment(attachment) || !attachment.context.isEmpty();
}

bool fileInfoIsSafeReferenceTarget(const QFileInfo &info, QString *canonicalPath = NULL)
{
  if(canonicalPath)
    canonicalPath->clear();
  if(!info.exists() || !info.isFile() || info.isSymLink())
    return false;
#if defined(Q_OS_WIN)
  const QString nativePath = QDir::toNativeSeparators(info.absoluteFilePath());
  const DWORD attributes = GetFileAttributesW(reinterpret_cast<LPCWSTR>(nativePath.utf16()));
  if(attributes == INVALID_FILE_ATTRIBUTES || (attributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0)
    return false;
#endif
  const QString resolved = info.canonicalFilePath();
  if(resolved.isEmpty() || resolved.size() > MaxAttachmentPathChars ||
     resolved.contains(QChar::Null) || resolved.contains(QLatin1Char('\r')) ||
     resolved.contains(QLatin1Char('\n')))
    return false;
  const QFileInfo resolvedInfo(resolved);
  if(!resolvedInfo.exists() || !resolvedInfo.isFile() || resolvedInfo.isSymLink())
    return false;
  if(canonicalPath)
    *canonicalPath = resolved;
  return true;
}

bool pathReferenceMatchesCurrentFile(const AIChatAttachment &attachment)
{
  if(!validPathReference(attachment))
    return false;
  QString canonicalPath;
  const QFileInfo info(attachment.content);
  if(!fileInfoIsSafeReferenceTarget(info, &canonicalPath) || canonicalPath != attachment.content)
    return false;
  return info.size() == attachment.originalBytes &&
         info.lastModified().toMSecsSinceEpoch() == attachment.modifiedUnixMs;
}

bool validRasterImageMediaType(const QString &mediaType)
{
  if(!validAttachmentMediaType(mediaType))
    return false;
  const QString normalized = attachmentMediaTypeEssence(mediaType);
  if(!normalized.startsWith(QLatin1String("image/")))
    return false;
  // Document/vector formats may load referenced resources. Keep them out of the in-process image
  // decoder; Windows may still supply a bounded, already-rasterized system thumbnail.
  // i18n-ignore: MIME types are stable attachment protocol identifiers.
  static const QSet<QString> documentImageTypes = {
      lit("image/svg+xml"),    // i18n-ignore: stable SVG MIME type
      lit("image/svg"),        // i18n-ignore: stable SVG MIME type
      lit("image/x-eps"),      // i18n-ignore: stable EPS MIME type
      lit("image/eps"),        // i18n-ignore: stable EPS MIME type
  };
  return !documentImageTypes.contains(normalized);
}

bool providerNativeImageMediaType(const QString &mediaType)
{
  const QString normalized = attachmentMediaTypeEssence(mediaType);
  // i18n-ignore: Provider-native raster MIME types are stable transport identifiers.
  static const QSet<QString> nativeTypes = {
      lit("image/png"), lit("image/jpeg"), lit("image/gif"),    // i18n-ignore: MIME IDs
      lit("image/webp"), lit("image/bmp"), lit("image/x-ms-bmp"),    // i18n-ignore: MIME IDs
  };
  return nativeTypes.contains(normalized);
}

bool isInlineRasterAttachment(const AIChatAttachment &attachment)
{
  return (attachment.kind == QLatin1String("image") ||
          attachment.kind == QLatin1String("file")) &&
         attachment.encoding == QLatin1String("base64") &&
         validRasterImageMediaType(attachment.mediaType);
}

bool decodeCanonicalBase64(const QString &content, QByteArray *decoded)
{
  if(decoded)
    decoded->clear();
  if(content.isEmpty() || content.size() > ((MaxBinaryAttachmentBytes + 2) / 3) * 4)
    return false;
  const QByteArray encoded = content.toLatin1();
  if(QString::fromLatin1(encoded) != content)
    return false;
  const QByteArray bytes = QByteArray::fromBase64(encoded);
  if(bytes.isEmpty() || bytes.size() > MaxBinaryAttachmentBytes || bytes.toBase64() != encoded)
    return false;
  if(decoded)
    *decoded = bytes;
  return true;
}

bool decodeSupportedTextBytes(const QByteArray &bytes, QString *text,
                              int maximumCharacters = MaxAttachmentContentChars,
                              bool allowTruncatedTail = false,
                              bool allowLocalEncoding = false)
{
  if(text)
    text->clear();
  if(bytes.isEmpty() || maximumCharacters <= 0)
    return false;

  QTextCodec *utf8 = QTextCodec::codecForName("UTF-8");    // i18n-ignore: text codec identifier
  QTextCodec *codec = QTextCodec::codecForUtfText(bytes, utf8);
  if(!codec)
    return false;
  // Unmarked text remains strict UTF-8. Embedded NUL bytes are admitted only when a UTF-16/32 BOM
  // selected the corresponding Unicode codec.
  if(codec == utf8 && bytes.contains('\0'))
    return false;

  const int maximumTrim = allowTruncatedTail ? qMin(8, bytes.size() - 1) : 0;
  auto decodeWithCodec = [&bytes, maximumTrim](QTextCodec *candidate, QString *decoded) {
    if(!candidate || !decoded)
      return false;
    for(int trim = 0; trim <= maximumTrim; ++trim)
    {
      QTextCodec::ConverterState state;
      const QString value = candidate->toUnicode(bytes.constData(), bytes.size() - trim, &state);
      if(state.invalidChars == 0)
      {
        *decoded = value;
        return true;
      }
    }
    return false;
  };

  QString decoded;
  bool valid = decodeWithCodec(codec, &decoded);
  if(!valid && allowLocalEncoding && codec == utf8)
  {
    // Text files selected on Windows commonly use the active ANSI code page. Only try the local
    // codec after MIME/type evidence has classified the input as text; arbitrary binary files
    // remain on the strict UTF-8 path above.
    QTextCodec *localCodec = QTextCodec::codecForLocale();
    if(localCodec != utf8)
    {
      valid = decodeWithCodec(localCodec, &decoded);
    }
  }
  if(!valid)
    return false;
  if(!decoded.isEmpty() && decoded.at(0) == QChar::ByteOrderMark)
    decoded.remove(0, 1);
  if(decoded.contains(QChar::Null) || decoded.trimmed().isEmpty())
    return false;
  if(decoded.size() > maximumCharacters)
    decoded = decoded.left(maximumCharacters);
  if(text)
    *text = decoded;
  return true;
}

bool attachmentDecodedBytes(const AIChatAttachment &attachment, qint64 *decodedBytes)
{
  if(decodedBytes)
    *decodedBytes = 0;
  if(isTextAttachment(attachment))
  {
    const qint64 bytes = attachment.content.toUtf8().size();
    if(decodedBytes)
      *decodedBytes = bytes;
    return true;
  }
  if(isPathReferenceAttachment(attachment))
  {
    if(!validPathReference(attachment))
      return false;
    if(decodedBytes)
      *decodedBytes = attachment.originalBytes;
    return true;
  }
  if(!isBinaryAttachment(attachment))
    return false;
  QByteArray bytes;
  if(!decodeCanonicalBase64(attachment.content, &bytes))
    return false;
  if(decodedBytes)
    *decodedBytes = bytes.size();
  return true;
}

QString rasterImageMediaTypeForFormat(const QByteArray &format)
{
  const QByteArray normalized = format.toLower();
  static const QSet<QByteArray> supportedFormats = []() {
    QSet<QByteArray> result;
    for(const QByteArray &candidate : QImageReader::supportedImageFormats())
      result.insert(candidate.toLower());
    return result;
  }();
  if(!supportedFormats.contains(normalized))
    return QString();

  // Never render document/vector codecs through this untrusted attachment path.
  if(normalized == "svg" || normalized == "svgz" || normalized == "pdf" ||
     normalized == "eps" || normalized == "ps")    // i18n-ignore: image codec identifiers
    return QString();
  if(normalized == "png")    // i18n-ignore: Qt image codec identifier
    return lit("image/png");    // i18n-ignore: MIME type
  if(normalized == "jpg" || normalized == "jpeg")    // i18n-ignore: Qt image codec identifiers
    return lit("image/jpeg");    // i18n-ignore: MIME type
  if(normalized == "gif")    // i18n-ignore: Qt image codec identifier
    return lit("image/gif");    // i18n-ignore: MIME type
  if(normalized == "webp")    // i18n-ignore: Qt image codec identifier
    return lit("image/webp");    // i18n-ignore: MIME type
  if(normalized == "bmp")    // i18n-ignore: Qt image codec identifier
    return lit("image/bmp");    // i18n-ignore: MIME type
  if(normalized == "ico" || normalized == "cur")    // i18n-ignore: codec identifiers
    return lit("image/x-icon");    // i18n-ignore: MIME type
  if(normalized == "tif" || normalized == "tiff")    // i18n-ignore: codec identifiers
    return lit("image/tiff");    // i18n-ignore: MIME type
  if(normalized == "tga")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-tga");    // i18n-ignore: MIME type
  if(normalized == "pbm")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-portable-bitmap");    // i18n-ignore: MIME type
  if(normalized == "pgm")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-portable-graymap");    // i18n-ignore: MIME type
  if(normalized == "ppm")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-portable-pixmap");    // i18n-ignore: MIME type
  if(normalized == "xbm")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-xbitmap");    // i18n-ignore: MIME type
  if(normalized == "xpm")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-xpixmap");    // i18n-ignore: MIME type
  if(normalized == "jp2" || normalized == "j2k" ||
     normalized == "jpx")    // i18n-ignore: codec identifiers
    return lit("image/jp2");    // i18n-ignore: MIME type
  if(normalized == "mng")    // i18n-ignore: Qt image codec identifier
    return lit("image/x-mng");    // i18n-ignore: MIME type
  if(normalized == "wbmp")    // i18n-ignore: Qt image codec identifier
    return lit("image/vnd.wap.wbmp");    // i18n-ignore: MIME type
  if(normalized == "avif")    // i18n-ignore: Qt image codec identifier
    return lit("image/avif");    // i18n-ignore: MIME type
  if(normalized == "heic" || normalized == "heif")    // i18n-ignore: codec identifiers
    return normalized == "heic" ? lit("image/heic")
                                  : lit("image/heif");    // i18n-ignore: MIME types

  // A dynamically installed Qt raster codec remains usable without a source-code MIME allow-list.
  // i18n-ignore: Constructed MIME type is attachment metadata, not visible prose.
  const QString fallback = lit("image/x-") + QString::fromLatin1(normalized);
  return validRasterImageMediaType(fallback) ? fallback : QString();
}

bool rasterImageReaderHeaderIsSafe(QImageReader &reader, QSize *imageSize = NULL,
                                   QString *detectedMediaType = NULL,
                                   QByteArray *detectedFormat = NULL)
{
  if(imageSize)
    *imageSize = QSize();
  if(detectedMediaType)
    detectedMediaType->clear();
  if(detectedFormat)
    detectedFormat->clear();
  reader.setDecideFormatFromContent(true);
  reader.setAutoTransform(true);
  const QByteArray format = reader.format().toLower();
  const QString mediaType = rasterImageMediaTypeForFormat(format);
  if(mediaType.isEmpty() || !reader.canRead())
    return false;
  const QSize size = reader.size();
  if(!size.isValid() || size.width() <= 0 || size.height() <= 0 ||
     size.width() > MaxImageDimension || size.height() > MaxImageDimension ||
     qint64(size.width()) * qint64(size.height()) > MaxImagePixels)
    return false;
  if(imageSize)
    *imageSize = size;
  if(detectedMediaType)
    *detectedMediaType = mediaType;
  if(detectedFormat)
    *detectedFormat = format;
  return true;
}

bool decodedRasterImageIsSafe(const QImage &image)
{
  return !image.isNull() && image.width() > 0 && image.height() > 0 &&
         image.width() <= MaxImageDimension && image.height() <= MaxImageDimension &&
         qint64(image.width()) * qint64(image.height()) <= MaxImagePixels;
}

bool binaryImageHeaderIsSafe(const QByteArray &bytes, const QString &mediaType,
                             QSize *imageSize = NULL, QString *detectedMediaType = NULL,
                             QByteArray *detectedFormat = NULL)
{
  if(imageSize)
    *imageSize = QSize();
  if(bytes.isEmpty() || bytes.size() > MaxBinaryAttachmentBytes ||
     !validRasterImageMediaType(mediaType))
    return false;

  QBuffer buffer;
  buffer.setData(bytes);
  if(!buffer.open(QIODevice::ReadOnly))
    return false;
  QImageReader reader(&buffer);
  QString actualMediaType;
  if(!rasterImageReaderHeaderIsSafe(reader, imageSize, &actualMediaType, detectedFormat))
    return false;
  if(actualMediaType != attachmentMediaTypeEssence(mediaType))
    return false;
  if(detectedMediaType)
    *detectedMediaType = actualMediaType;
  return true;
}

bool rasterImageBytesHeaderIsSafe(const QByteArray &bytes, QSize *imageSize = NULL,
                                  QString *detectedMediaType = NULL,
                                  QByteArray *detectedFormat = NULL)
{
  if(imageSize)
    *imageSize = QSize();
  if(detectedMediaType)
    detectedMediaType->clear();
  if(detectedFormat)
    detectedFormat->clear();
  if(bytes.isEmpty() || bytes.size() > MaxBinaryAttachmentBytes)
    return false;

  QBuffer buffer;
  buffer.setData(bytes);
  if(!buffer.open(QIODevice::ReadOnly))
    return false;
  QImageReader reader(&buffer);
  return rasterImageReaderHeaderIsSafe(reader, imageSize, detectedMediaType, detectedFormat);
}

bool pathReferenceImageHeaderIsSafe(const AIChatAttachment &attachment,
                                    QSize *imageSize = NULL,
                                    QString *detectedMediaType = NULL)
{
  if(!pathReferenceMatchesCurrentFile(attachment) ||
     !validRasterImageMediaType(attachment.mediaType))
    return false;
  const QFileInfo info(attachment.content);
  // Preview decoders must never be handed an unbounded path-reference stream. This separate cap
  // still permits ordinary images larger than the 8 MiB inline transport budget to retain their
  // thumbnail while exceptionally large files remain path-only references.
  if(!info.exists() || !info.isFile() || info.size() != attachment.originalBytes ||
     info.size() > MaxPathReferenceImagePreviewBytes)
    return false;
  QImageReader reader(info.absoluteFilePath());
  QString mediaType;
  if(!rasterImageReaderHeaderIsSafe(reader, imageSize, &mediaType))
    return false;
  if(!pathReferenceMatchesCurrentFile(attachment))
    return false;
  if(detectedMediaType)
    *detectedMediaType = mediaType;
  return true;
}

bool attachmentPreviewBytes(const AIChatAttachment &attachment, QByteArray *bytes)
{
  if(bytes)
    bytes->clear();

  if(isInlineRasterAttachment(attachment))
    return decodeCanonicalBase64(attachment.content, bytes);

  if(!isPathReferenceAttachment(attachment) || !pathReferenceMatchesCurrentFile(attachment) ||
     !validRasterImageMediaType(attachment.mediaType))
    return false;

  const QFileInfo info(attachment.content);
  if(info.size() <= 0 || info.size() > MaxPathReferenceAnimatedImageBytes)
    return false;
  QFile file(info.absoluteFilePath());
  if(!file.open(QIODevice::ReadOnly))
    return false;
  const QByteArray data = file.read(MaxPathReferenceAnimatedImageBytes + 1);
  if(data.size() != info.size() || data.size() > MaxPathReferenceAnimatedImageBytes)
    return false;
  if(!pathReferenceMatchesCurrentFile(attachment))
    return false;
  if(bytes)
    *bytes = data;
  return true;
}

bool attachmentAnimationIsSafe(const AIChatAttachment &attachment, QByteArray *bytes = NULL,
                               QByteArray *format = NULL)
{
  QByteArray encoded;
  if(!attachmentPreviewBytes(attachment, &encoded))
    return false;
  QBuffer buffer;
  buffer.setData(encoded);
  if(!buffer.open(QIODevice::ReadOnly))
    return false;
  QImageReader reader(&buffer);
  QSize size;
  QString mediaType;
  if(!rasterImageReaderHeaderIsSafe(reader, &size, &mediaType) ||
     mediaType != attachmentMediaTypeEssence(attachment.mediaType))
    return false;
  const int frames = reader.imageCount();
  if(frames <= 1 || frames > MaxAnimatedImageFrames ||
     qint64(frames) * qint64(size.width()) * qint64(size.height()) >
         MaxAnimatedImagePixelFrames)
    return false;
  if(bytes)
    *bytes = encoded;
  if(format)
    *format = reader.format().toLower();
  return true;
}

QMovie *safeAttachmentMovie(const AIChatAttachment &attachment, const QSize &bounds,
                            QWidget *parent)
{
  if(!bounds.isValid())
    return NULL;

  QByteArray bytes;
  QByteArray format;
  if(!attachmentAnimationIsSafe(attachment, &bytes, &format))
    return NULL;

  QBuffer *buffer = new QBuffer;
  buffer->setData(bytes);
  if(!buffer->open(QIODevice::ReadOnly))
  {
    delete buffer;
    return NULL;
  }

  QMovie *movie = new QMovie(buffer, format, parent);
  // QMovie does not own the QIODevice passed to it. Make the ownership explicit so a chip or
  // preview dialog cannot leave the movie pointing at a destroyed stack QBuffer.
  buffer->setParent(movie);
  // Both the composer and preview can animate without retaining every decoded frame. Keeping only
  // the current frame prevents several simultaneous GIF chips from multiplying memory use.
  movie->setCacheMode(QMovie::CacheNone);
  QSize imageSize;
  if(!pathReferenceImageHeaderIsSafe(attachment, &imageSize) &&
     !(isInlineRasterAttachment(attachment) &&
       binaryImageHeaderIsSafe(bytes, attachment.mediaType, &imageSize)))
  {
    delete movie;
    return NULL;
  }
  movie->setScaledSize(imageSize.scaled(bounds, Qt::KeepAspectRatio));
  if(!movie->isValid())
  {
    delete movie;
    return NULL;
  }
  return movie;
}

QPixmap attachmentThumbnail(const AIChatAttachment &attachment, const QSize &bounds)
{
  if(!bounds.isValid())
    return QPixmap();
  QImage image;
  if(isInlineRasterAttachment(attachment))
  {
    QByteArray bytes;
    QSize imageSize;
    if(!decodeCanonicalBase64(attachment.content, &bytes) ||
       !binaryImageHeaderIsSafe(bytes, attachment.mediaType, &imageSize))
      return QPixmap();
    QBuffer buffer;
    buffer.setData(bytes);
    if(!buffer.open(QIODevice::ReadOnly))
      return QPixmap();
    QImageReader reader(&buffer);
    reader.setDecideFormatFromContent(true);
    reader.setAutoTransform(true);
    reader.setScaledSize(imageSize.scaled(bounds, Qt::KeepAspectRatio));
    image = reader.read();
  }
  else if(isPathReferenceAttachment(attachment))
  {
    QSize imageSize;
    if(!pathReferenceImageHeaderIsSafe(attachment, &imageSize))
      return QPixmap();
    QImageReader reader(attachment.content);
    reader.setDecideFormatFromContent(true);
    reader.setAutoTransform(true);
    reader.setScaledSize(imageSize.scaled(bounds, Qt::KeepAspectRatio));
    image = reader.read();
    if(!pathReferenceMatchesCurrentFile(attachment))
      return QPixmap();
  }
  if(!decodedRasterImageIsSafe(image))
    return QPixmap();
  return QPixmap::fromImage(std::move(image));
}

bool attachmentTextPreview(const AIChatAttachment &attachment, QString *text,
                           int maximumCharacters = MaxTextThumbnailChars)
{
  if(text)
    text->clear();
  if(maximumCharacters <= 0 || !validTextAttachmentMediaType(attachment.mediaType))
    return false;

  if(isTextAttachment(attachment))
  {
    if(attachment.content.trimmed().isEmpty())
      return false;
    if(text)
      *text = attachment.content.left(maximumCharacters);
    return true;
  }

  QByteArray bytes;
  bool allowTruncatedTail = false;
  if(attachment.kind == QLatin1String("file"))
  {
    if(!decodeCanonicalBase64(attachment.content, &bytes))
      return false;
  }
  else if(isPathReferenceAttachment(attachment))
  {
    if(!pathReferenceMatchesCurrentFile(attachment))
      return false;
    QFile file(attachment.content);
    if(!file.open(QIODevice::ReadOnly))
      return false;
    bytes = file.read(MaxTextThumbnailReadBytes);
    allowTruncatedTail = file.size() > bytes.size();
    if(bytes.isEmpty() || !pathReferenceMatchesCurrentFile(attachment))
      return false;
  }
  else
  {
    return false;
  }

  return decodeSupportedTextBytes(bytes, text, maximumCharacters, allowTruncatedTail, true);
}

QPixmap textAttachmentThumbnail(const QString &text, const QSize &bounds,
                                const QPalette &palette)
{
  if(text.isEmpty() || !bounds.isValid())
    return QPixmap();

  const QSize canvasSize(qMax(48, bounds.width()), qMax(48, bounds.height()));
  QImage image(canvasSize, QImage::Format_ARGB32_Premultiplied);
  image.fill(palette.color(QPalette::Base));

  QPainter painter(&image);
  painter.setPen(palette.color(QPalette::Text));
  QFont font = QFontDatabase::systemFont(QFontDatabase::FixedFont);
  font.setPixelSize(qMax(7, canvasSize.height() / 9));
  painter.setFont(font);
  const QFontMetrics metrics(font);
  const int margin = qMax(3, canvasSize.width() / 18);
  const int lineHeight = qMax(1, metrics.lineSpacing());
  const int maximumLines = qMax(1, (canvasSize.height() - 2 * margin) / lineHeight);
  const int textWidth = qMax(1, canvasSize.width() - 2 * margin);
  const QStringList lines = text.split(QLatin1Char('\n'));
  int y = margin + metrics.ascent();
  for(int line = 0; line < maximumLines && line < lines.size(); ++line)
  {
    QString visibleLine = lines[line];
    visibleLine.replace(QLatin1Char('\t'), lit("  "));    // i18n-ignore: whitespace rendering
    visibleLine = metrics.elidedText(visibleLine, Qt::ElideRight, textWidth);
    painter.drawText(margin, y, visibleLine);
    y += lineHeight;
  }
  painter.end();
  return QPixmap::fromImage(image.scaled(bounds, Qt::KeepAspectRatio,
                                         Qt::SmoothTransformation));
}

bool attachmentImageSize(const AIChatAttachment &attachment, QSize *imageSize)
{
  if(imageSize)
    *imageSize = QSize();

  if(isInlineRasterAttachment(attachment))
  {
    QByteArray bytes;
    QSize size;
    if(!decodeCanonicalBase64(attachment.content, &bytes) ||
       !binaryImageHeaderIsSafe(bytes, attachment.mediaType, &size))
      return false;
    if(imageSize)
      *imageSize = size;
    return true;
  }

  return isPathReferenceAttachment(attachment) &&
         pathReferenceImageHeaderIsSafe(attachment, imageSize);
}

bool attachmentCanUseSystemRasterDecoder(const AIChatAttachment &attachment)
{
  if(!validRasterImageMediaType(attachment.mediaType))
    return false;

  if(attachment.kind == QLatin1String("file") &&
     attachment.encoding == QLatin1String("base64"))
  {
    return attachment.originalBytes > 0 &&
           attachment.originalBytes <= MaxBinaryAttachmentBytes &&
           !attachment.content.isEmpty() &&
           attachment.content.size() <= ((MaxBinaryAttachmentBytes + 2) / 3) * 4;
  }

  return isPathReferenceAttachment(attachment) &&
         attachment.originalBytes <= MaxPathReferenceImagePreviewBytes &&
         pathReferenceMatchesCurrentFile(attachment);
}

AIFileThumbnail::RasterDecodeResult attachmentSystemRasterPreview(
    const AIChatAttachment &attachment, const QSize &maximumSize = QSize())
{
  if(!attachmentCanUseSystemRasterDecoder(attachment))
    return AIFileThumbnail::RasterDecodeResult();

  if(attachment.kind == QLatin1String("file"))
  {
    QByteArray bytes;
    if(!decodeCanonicalBase64(attachment.content, &bytes))
      return AIFileThumbnail::RasterDecodeResult();
    return AIFileThumbnail::decodeRasterData(bytes, maximumSize, MaxBinaryAttachmentBytes);
  }

  AIFileThumbnail::RasterDecodeResult result = AIFileThumbnail::decodeRasterFile(
      attachment.content, maximumSize, MaxPathReferenceImagePreviewBytes);
  if(!pathReferenceMatchesCurrentFile(attachment))
    return AIFileThumbnail::RasterDecodeResult();
  return result;
}

AIFileThumbnail::RasterAnimationResult attachmentSystemRasterAnimation(
    const AIChatAttachment &attachment, const QSize &maximumSize = QSize())
{
  if(!attachmentCanUseSystemRasterDecoder(attachment))
    return AIFileThumbnail::RasterAnimationResult();

  if(attachment.kind == QLatin1String("file"))
  {
    QByteArray bytes;
    if(!decodeCanonicalBase64(attachment.content, &bytes))
      return AIFileThumbnail::RasterAnimationResult();
    return AIFileThumbnail::decodeGIFAnimationData(
        bytes, maximumSize, MaxBinaryAttachmentBytes, MaxAnimatedImageFrames,
        MaxAnimatedImagePixelFrames);
  }

  if(attachment.originalBytes > MaxPathReferenceAnimatedImageBytes)
    return AIFileThumbnail::RasterAnimationResult();
  AIFileThumbnail::RasterAnimationResult result = AIFileThumbnail::decodeGIFAnimationFile(
      attachment.content, maximumSize, MaxPathReferenceAnimatedImageBytes,
      MaxAnimatedImageFrames, MaxAnimatedImagePixelFrames);
  if(!pathReferenceMatchesCurrentFile(attachment))
    return AIFileThumbnail::RasterAnimationResult();
  return result;
}

QPixmap attachmentPreviewPixmap(const AIChatAttachment &attachment)
{
  QImage image;
  if(isInlineRasterAttachment(attachment))
  {
    QByteArray bytes;
    QSize imageSize;
    if(!decodeCanonicalBase64(attachment.content, &bytes) ||
       !binaryImageHeaderIsSafe(bytes, attachment.mediaType, &imageSize))
      return QPixmap();
    QBuffer buffer;
    buffer.setData(bytes);
    if(!buffer.open(QIODevice::ReadOnly))
      return QPixmap();
    QImageReader reader(&buffer);
    reader.setDecideFormatFromContent(true);
    reader.setAutoTransform(true);
    image = reader.read();
  }
  else if(isPathReferenceAttachment(attachment))
  {
    QSize imageSize;
    if(!pathReferenceImageHeaderIsSafe(attachment, &imageSize))
      return QPixmap();
    QImageReader reader(attachment.content);
    reader.setDecideFormatFromContent(true);
    reader.setAutoTransform(true);
    image = reader.read();
    // A path-reference preview is only valid for the exact file that was attached. Revalidate
    // after decoding as well as before it so a changed file is not silently shown as that
    // attachment. This does not broaden the existing path-reference read boundary.
    if(!pathReferenceMatchesCurrentFile(attachment))
      return QPixmap();
  }

  if(!decodedRasterImageIsSafe(image))
    return QPixmap();
  return QPixmap::fromImage(std::move(image));
}

class AttachmentImagePreview : public QWidget
{
public:
  explicit AttachmentImagePreview(QWidget *parent = NULL) : QWidget(parent)
  {
    setFocusPolicy(Qt::StrongFocus);
    setCursor(Qt::OpenHandCursor);
    setAutoFillBackground(false);
  }

  QSize sizeHint() const override { return QSize(640, 420); }
  QSize minimumSizeHint() const override { return QSize(240, 160); }

  void setPixmap(const QPixmap &pixmap)
  {
    m_Movie = NULL;
    m_Frame = pixmap;
    m_SourceSize = pixmap.size();
    m_UserAdjusted = false;
    resetView();
  }

  void setMovie(QMovie *movie, const QSize &sourceSize)
  {
    m_Movie = movie;
    m_SourceSize = sourceSize;
    m_UserAdjusted = false;
    if(!m_Movie)
    {
      m_Frame = QPixmap();
      update();
      return;
    }

    QObject::connect(m_Movie, &QMovie::frameChanged, this, [this](int) {
      if(m_Movie)
      {
        m_Frame = m_Movie->currentPixmap();
        update();
      }
    });
    m_Movie->start();
    m_Frame = m_Movie->currentPixmap();
    resetView();
  }

  void setRasterAnimationFrame(const QImage &image, const QSize &sourceSize, bool firstFrame)
  {
    const QPixmap pixmap = QPixmap::fromImage(image);
    if(pixmap.isNull() || !sourceSize.isValid())
      return;
    m_Movie = NULL;
    m_Frame = pixmap;
    if(firstFrame || m_SourceSize != sourceSize)
    {
      m_SourceSize = sourceSize;
      m_UserAdjusted = false;
      resetView();
    }
    else
    {
      update();
    }
  }

protected:
  void paintEvent(QPaintEvent *event) override
  {
    Q_UNUSED(event);
    QPainter painter(this);
    painter.fillRect(rect(), palette().brush(QPalette::Base));
    if(m_Frame.isNull() || !m_SourceSize.isValid())
      return;

    painter.setRenderHint(QPainter::SmoothPixmapTransform, true);
    const QSizeF renderedSize(qreal(m_SourceSize.width()) * m_Scale,
                              qreal(m_SourceSize.height()) * m_Scale);
    const QPointF topLeft = viewportCenter() + m_Pan -
                            QPointF(renderedSize.width() / 2.0, renderedSize.height() / 2.0);
    painter.drawPixmap(QRectF(topLeft, renderedSize), m_Frame, QRectF(m_Frame.rect()));
  }

  void resizeEvent(QResizeEvent *event) override
  {
    QWidget::resizeEvent(event);
    if(m_UserAdjusted)
    {
      constrainPan();
      update();
    }
    else
    {
      resetView();
    }
  }

  void wheelEvent(QWheelEvent *event) override
  {
    if(m_Frame.isNull() || !m_SourceSize.isValid())
    {
      QWidget::wheelEvent(event);
      return;
    }

    qreal steps = qreal(event->angleDelta().y()) / 120.0;
    if(qFuzzyIsNull(steps))
      steps = qreal(event->pixelDelta().y()) / 120.0;
    if(qFuzzyIsNull(steps))
    {
      event->ignore();
      return;
    }

    const qreal oldScale = m_Scale;
    const qreal nextScale = qBound(minimumScale(),
                                   oldScale * std::pow(1.2, double(steps)), qreal(16.0));
    if(qFuzzyCompare(oldScale, nextScale))
    {
      event->accept();
      return;
    }

    const QPointF cursorPosition(event->pos());
    const QSizeF oldSize(qreal(m_SourceSize.width()) * oldScale,
                         qreal(m_SourceSize.height()) * oldScale);
    const QPointF oldTopLeft = viewportCenter() + m_Pan -
                               QPointF(oldSize.width() / 2.0, oldSize.height() / 2.0);
    const QPointF sourcePosition = (cursorPosition - oldTopLeft) / oldScale;
    const QSizeF nextSize(qreal(m_SourceSize.width()) * nextScale,
                          qreal(m_SourceSize.height()) * nextScale);
    const QPointF nextTopLeft = cursorPosition - sourcePosition * nextScale;

    m_Scale = nextScale;
    m_Pan = nextTopLeft - viewportCenter() +
            QPointF(nextSize.width() / 2.0, nextSize.height() / 2.0);
    m_UserAdjusted = true;
    constrainPan();
    update();
    event->accept();
  }

  void mousePressEvent(QMouseEvent *event) override
  {
    if(event->button() == Qt::LeftButton && !m_Frame.isNull())
    {
      m_Dragging = true;
      m_LastMousePosition = event->pos();
      setCursor(Qt::ClosedHandCursor);
      event->accept();
      return;
    }
    QWidget::mousePressEvent(event);
  }

  void mouseMoveEvent(QMouseEvent *event) override
  {
    if(m_Dragging)
    {
      const QPoint position = event->pos();
      m_Pan += position - m_LastMousePosition;
      m_LastMousePosition = position;
      m_UserAdjusted = true;
      constrainPan();
      update();
      event->accept();
      return;
    }
    QWidget::mouseMoveEvent(event);
  }

  void mouseReleaseEvent(QMouseEvent *event) override
  {
    if(event->button() == Qt::LeftButton && m_Dragging)
    {
      m_Dragging = false;
      setCursor(Qt::OpenHandCursor);
      event->accept();
      return;
    }
    QWidget::mouseReleaseEvent(event);
  }

private:
  QPointF viewportCenter() const { return QPointF(width() / 2.0, height() / 2.0); }

  qreal fitScale() const
  {
    if(!m_SourceSize.isValid() || width() <= 0 || height() <= 0)
      return 1.0;
    return qMin(qreal(width()) / qreal(m_SourceSize.width()),
                qreal(height()) / qreal(m_SourceSize.height()));
  }

  qreal minimumScale() const { return qMax(qreal(0.001), qMin(qreal(1.0), fitScale()) * 0.25); }

  void resetView()
  {
    if(!m_SourceSize.isValid() || width() <= 0 || height() <= 0)
      return;
    m_Scale = qMin(qreal(1.0), fitScale());
    m_Pan = QPointF();
    update();
  }

  void constrainPan()
  {
    if(!m_SourceSize.isValid() || width() <= 0 || height() <= 0)
      return;

    const qreal renderedWidth = qreal(m_SourceSize.width()) * m_Scale;
    const qreal renderedHeight = qreal(m_SourceSize.height()) * m_Scale;
    const qreal visibleWidth = qMin(qreal(32.0), renderedWidth);
    const qreal visibleHeight = qMin(qreal(32.0), renderedHeight);
    const QPointF center = viewportCenter();
    const qreal minimumX = visibleWidth - center.x() - renderedWidth / 2.0;
    const qreal maximumX = width() - visibleWidth - center.x() + renderedWidth / 2.0;
    const qreal minimumY = visibleHeight - center.y() - renderedHeight / 2.0;
    const qreal maximumY = height() - visibleHeight - center.y() + renderedHeight / 2.0;
    m_Pan.setX(qBound(minimumX, m_Pan.x(), maximumX));
    m_Pan.setY(qBound(minimumY, m_Pan.y(), maximumY));
  }

  QMovie *m_Movie = NULL;
  QPixmap m_Frame;
  QSize m_SourceSize;
  qreal m_Scale = 1.0;
  QPointF m_Pan;
  QPoint m_LastMousePosition;
  bool m_Dragging = false;
  bool m_UserAdjusted = false;
};

class AttachmentFrameAnimation : public QObject
{
public:
  AttachmentFrameAnimation(const QVector<QImage> &frames, const QVector<int> &frameDelaysMs,
                           QObject *parent, std::function<void(const QImage &, bool)> callback)
      : QObject(parent),
        m_Frames(frames),
        m_FrameDelaysMs(frameDelaysMs),
        m_Callback(std::move(callback))
  {
    m_Timer.setSingleShot(true);
    QObject::connect(&m_Timer, &QTimer::timeout, this, [this]() { advance(); });
  }

  void start()
  {
    if(m_Frames.size() < 2 || m_FrameDelaysMs.size() != m_Frames.size() || !m_Callback)
      return;
    m_Index = 0;
    m_Callback(m_Frames[0], true);
    m_Timer.start(qBound(20, m_FrameDelaysMs[0], 10000));
  }

private:
  void advance()
  {
    if(m_Frames.size() < 2 || m_FrameDelaysMs.size() != m_Frames.size() || !m_Callback)
      return;
    m_Index = (m_Index + 1) % m_Frames.size();
    m_Callback(m_Frames[m_Index], false);
    m_Timer.start(qBound(20, m_FrameDelaysMs[m_Index], 10000));
  }

  QVector<QImage> m_Frames;
  QVector<int> m_FrameDelaysMs;
  std::function<void(const QImage &, bool)> m_Callback;
  QTimer m_Timer;
  int m_Index = 0;
};

bool attachmentHasSafeImagePreview(const AIChatAttachment &attachment)
{
  if(isInlineRasterAttachment(attachment))
  {
    QByteArray bytes;
    return decodeCanonicalBase64(attachment.content, &bytes) &&
           binaryImageHeaderIsSafe(bytes, attachment.mediaType);
  }
  return isPathReferenceAttachment(attachment) && pathReferenceImageHeaderIsSafe(attachment);
}

bool attachmentsNeedV4(const QVector<AIChatAttachment> &attachments)
{
  for(const AIChatAttachment &attachment : attachments)
    if(attachmentRequiresV4(attachment))
      return true;
  return false;
}

bool attachmentsNeedV5(const QVector<AIChatAttachment> &attachments)
{
  for(const AIChatAttachment &attachment : attachments)
    if(attachmentRequiresV5(attachment))
      return true;
  return false;
}

bool schemaSupportsAttachments(const QStringList &schemas,
                                const QVector<AIChatAttachment> &attachments)
{
  if(attachments.isEmpty())
    return true;
  const bool supportsV5 =
      schemas.contains(lit("replaylab-conversation/v5"));    // i18n-ignore: wire schema ID
  if(attachmentsNeedV5(attachments))
    return supportsV5;
  const bool supportsV4 =
      schemas.contains(lit("replaylab-conversation/v4"));    // i18n-ignore: wire schema ID
  if(attachmentsNeedV4(attachments))
    return supportsV5 || supportsV4;
  return supportsV5 || supportsV4 ||
         schemas.contains(lit("replaylab-conversation/v3"));    // i18n-ignore: wire schema ID
}

bool chatAttachmentsCouldFitPersistenceBudget(
    const QVector<AIChatAttachment> &attachments, int remainingChars)
{
  if(remainingChars <= 0)
    return attachments.isEmpty();

  // This is only a cheap lower-bound rejection before constructing a potentially multi-megabyte
  // QJson tree. Every UTF-16 value character occupies at least one output byte, and the stable
  // field names, quotes, separators, numbers and booleans occupy comfortably more than 96 bytes
  // per row. Returning true is not acceptance: the existing compact-JSON size check remains
  // authoritative for escaping, UTF-8 expansion and context objects.
  qint64 minimumBytes = 2;    // JSON array brackets
  for(const AIChatAttachment &attachment : attachments)
  {
    minimumBytes += 96;
    minimumBytes += attachment.id.size();
    minimumBytes += attachment.kind.size();
    minimumBytes += attachment.title.size();
    minimumBytes += attachment.mediaType.size();
    minimumBytes += attachment.source.size();
    minimumBytes += attachment.encoding.size();
    minimumBytes += attachment.content.size();
    if(!attachment.context.isEmpty())
      minimumBytes += 12;    // context field name plus the minimum object delimiters
    if(minimumBytes > remainingChars)
      return false;
  }
  return true;
}

QJsonObject chatAttachmentToJSON(const AIChatAttachment &attachment)
{
  QJsonObject object;
  // i18n-ignore: Attachment fields and trust marker are ReplayLab protocol identifiers.
  object[lit("id")] = attachment.id;
  object[lit("kind")] = attachment.kind;    // i18n-ignore: attachment protocol field
  object[lit("title")] = attachment.title;    // i18n-ignore: attachment protocol field
  object[lit("mediaType")] = attachment.mediaType;    // i18n-ignore: attachment protocol field
  object[lit("source")] = attachment.source;    // i18n-ignore: attachment protocol field
  object[lit("encoding")] = attachment.encoding;    // i18n-ignore: attachment protocol field
  object[lit("content")] = attachment.content;    // i18n-ignore: attachment protocol field
  if(!attachment.context.isEmpty())
    object[lit("context")] = attachment.context;    // i18n-ignore: attachment protocol field
  object[lit("originalBytes")] = double(attachment.originalBytes);    // i18n-ignore: attachment protocol field
  if(attachment.modifiedUnixMs > 0)
    // i18n-ignore: Attachment protocol field is a stable JSON key.
    object[lit("modifiedUnixMs")] = double(attachment.modifiedUnixMs);
  object[lit("truncated")] = attachment.truncated;    // i18n-ignore: attachment protocol field
  object[lit("trust")] = lit("untrusted");    // i18n-ignore: attachment protocol field/value
  return object;
}

QJsonArray chatAttachmentsToJSON(const QVector<AIChatAttachment> &attachments)
{
  QJsonArray array;
  for(const AIChatAttachment &attachment : attachments)
    array.push_back(chatAttachmentToJSON(attachment));
  return array;
}

bool chatAttachmentFromJSON(const QJsonObject &object, AIChatAttachment &attachment)
{
  AIChatAttachment value;
  // i18n-ignore: Persisted attachment fields and values are stable protocol identifiers.
  value.id = object.value(lit("id")).toString();
  value.kind = object.value(lit("kind")).toString();    // i18n-ignore: attachment protocol field
  value.title = object.value(lit("title")).toString();    // i18n-ignore: attachment protocol field
  value.mediaType = object.value(lit("mediaType")).toString();    // i18n-ignore: attachment protocol field
  value.source = object.value(lit("source")).toString();    // i18n-ignore: attachment protocol field
  value.encoding = object.value(lit("encoding")).toString();    // i18n-ignore: attachment protocol field
  value.content = object.value(lit("content")).toString();    // i18n-ignore: attachment protocol field
  const QJsonValue contextValue = object.value(lit("context"));    // i18n-ignore: attachment protocol field
  if(!contextValue.isUndefined() && !contextValue.isObject())
    return false;
  value.context = contextValue.toObject();
  // Versions before 1.13.46 wrote a numeric uint64 Shader ID for workspace selections.
  // Its low bits may already have been lost. Keep the selected text/history, discard those
  // ambiguous identity hints instead of converting the rounded number back to a false ID.
  QJsonObject legacyShader = value.context.value(QLatin1String("shader")).toObject();
  if(legacyShader.value(QLatin1String("resourceId")).isDouble() &&
     value.context.value(QLatin1String("origin")).toObject()
             .value(QLatin1String("kind")).toString() == QLatin1String("shader-workspace"))
  {
    legacyShader.remove(QLatin1String("resourceId"));
    legacyShader.remove(QLatin1String("rawSha256"));
    value.context[QLatin1String("shader")] = legacyShader;
    value.context.remove(QLatin1String("relations"));
    QJsonObject capture = value.context.value(QLatin1String("capture")).toObject();
    capture.remove(QLatin1String("captureSessionId"));
    value.context[QLatin1String("capture")] = capture;
  }
  value.originalBytes = object.value(lit("originalBytes")).toVariant().toLongLong();    // i18n-ignore: attachment protocol field
  value.modifiedUnixMs =
      object.value(lit("modifiedUnixMs")).toVariant().toLongLong();    // i18n-ignore: attachment protocol field
  value.truncated = object.value(lit("truncated")).toBool();    // i18n-ignore: attachment protocol field
  const QString trust = object.value(lit("trust")).toString(lit("untrusted"));    // i18n-ignore: attachment protocol field/value
  if(!safeTaskId(value.id) || !validAttachmentKind(value.kind) || value.title.isEmpty() ||
     value.title.size() > 512 || value.title.contains(QChar::Null) ||
     value.title.contains(QLatin1Char('\n')) || value.title.contains(QLatin1Char('\r')) ||
     value.mediaType.size() > 160 || !validAttachmentMediaType(value.mediaType) ||
     value.source.size() > 32768 || value.source.contains(QChar::Null) ||
     value.source.contains(QLatin1Char('\n')) || value.source.contains(QLatin1Char('\r')) ||
     value.content.isEmpty() || value.content.contains(QChar::Null) || value.originalBytes <= 0 ||
     !validAttachmentContext(value.context) ||
     value.truncated || trust != lit("untrusted"))    // i18n-ignore: attachment trust marker
    return false;

  qint64 decodedBytes = 0;
  if(!attachmentDecodedBytes(value, &decodedBytes) || value.originalBytes != decodedBytes)
    return false;
  if(isPathReferenceAttachment(value))
  {
    if(!validPathReference(value))
      return false;
  }
  else if(isTextAttachment(value))
  {
    if(value.encoding != QLatin1String("utf-8") ||
       !validTextAttachmentMediaType(value.mediaType) ||
       value.content.size() > MaxAttachmentContentChars || decodedBytes > MaxAttachmentUTF8Bytes)
      return false;
  }
  else
  {
    if(value.encoding != QLatin1String("base64") || decodedBytes > MaxBinaryAttachmentBytes)
      return false;
    if(value.kind == QLatin1String("image"))
    {
      QByteArray bytes;
      if(!validRasterImageMediaType(value.mediaType) ||
         !decodeCanonicalBase64(value.content, &bytes) ||
         !binaryImageHeaderIsSafe(bytes, value.mediaType))
        return false;
    }
  }
  attachment = value;
  return true;
}

QVector<AIChatAttachment> chatAttachmentsFromJSON(const QJsonValue &value)
{
  QVector<AIChatAttachment> attachments;
  if(!value.isArray())
    return attachments;
  const QJsonArray array = value.toArray();
  if(array.size() > MaxChatAttachments)
    return attachments;
  qint64 totalTextBytes = 0;
  qint64 totalBinaryBytes = 0;
  for(const QJsonValue &entry : array)
  {
    if(!entry.isObject())
      return QVector<AIChatAttachment>();
    AIChatAttachment attachment;
    if(!chatAttachmentFromJSON(entry.toObject(), attachment))
      return QVector<AIChatAttachment>();
    qint64 decodedBytes = 0;
    if(!attachmentDecodedBytes(attachment, &decodedBytes))
      return QVector<AIChatAttachment>();
    if(isTextAttachment(attachment))
      totalTextBytes += decodedBytes;
    else if(isBinaryAttachment(attachment))
      totalBinaryBytes += decodedBytes;
    if(totalTextBytes > MaxAttachmentTotalUTF8Bytes ||
       totalBinaryBytes > MaxBinaryAttachmentTotalBytes)
      return QVector<AIChatAttachment>();
    attachments.push_back(attachment);
  }
  return attachments;
}

QString environmentValue(const char *name)
{
  // qgetenv() exposes a narrow QByteArray and QFile::decodeName() follows the
  // process locale. On Windows that can lose a Chinese user/profile or CLI
  // installation path before QProcess ever gets a chance to use CreateProcessW.
  // QProcessEnvironment reads the native Unicode environment and preserves the
  // exact QString value end to end.
  return QProcessEnvironment::systemEnvironment().value(QString::fromLatin1(name));
}

QSet<QString> knownModelCredentialEnvironments()
{
  // Deny known provider credentials even when the user has not configured that provider in
  // ReplayLab. A managed subscription process must never inherit an unrelated model API key
  // merely because qrenderdoc was launched from a developer shell containing it.
  static const char *const known[] = {
      "OPENAI_API_KEY",       "ANTHROPIC_API_KEY",  "GOOGLE_API_KEY",
      "GEMINI_API_KEY",      "AZURE_OPENAI_API_KEY", "OPENROUTER_API_KEY",
      "XAI_API_KEY",          "MISTRAL_API_KEY",    "GROQ_API_KEY",
      "DEEPSEEK_API_KEY",    "DASHSCOPE_API_KEY",  "MOONSHOT_API_KEY",
      "MINIMAX_API_KEY",     "ZHIPUAI_API_KEY",    "ARK_API_KEY",
      "HUNYUAN_API_KEY",     "QIANFAN_API_KEY",    "SILICONFLOW_API_KEY",
      "REPLAYLAB_CUSTOM_API_KEY",
  };
  QSet<QString> result;
  for(const char *name : known)
    result.insert(QString::fromLatin1(name));
  return result;
}

QJsonDocument parseJSONOrLastJSONLRecord(const QByteArray &output)
{
  const QByteArray trimmed = output.trimmed();
  QJsonParseError parseError;
  QJsonDocument document = QJsonDocument::fromJson(trimmed, &parseError);
  if(parseError.error == QJsonParseError::NoError && !document.isNull())
    return document;

  // Machine commands normally return one JSON value, while long-running subscription commands
  // use JSONL. Accept the last complete record for status-style calls so a backend may add
  // progress records without forcing qrenderdoc to special-case the transport.
  const QList<QByteArray> lines = trimmed.split('\n');
  for(int i = lines.count() - 1; i >= 0; --i)
  {
    const QByteArray line = lines[i].trimmed();
    if(line.isEmpty())
      continue;
    document = QJsonDocument::fromJson(line, &parseError);
    if(parseError.error == QJsonParseError::NoError && !document.isNull())
      return document;
  }
  return QJsonDocument();
}

void deleteProcessWhenFinished(QProcess *process)
{
  QObject::connect(process, &QProcess::stateChanged, process,
                   [process](QProcess::ProcessState state) {
                     if(state == QProcess::NotRunning)
                       process->deleteLater();
                   });
  QObject::connect(
      process, static_cast<void (QProcess::*)(int, QProcess::ExitStatus)>(&QProcess::finished),
      process, &QObject::deleteLater);
  // Close the state-check/connection race if the process exited between the
  // caller's check and these signal connections.
  if(process->state() == QProcess::NotRunning)
    process->deleteLater();
}

class FunctionRunnable : public QRunnable
{
public:
  explicit FunctionRunnable(std::function<void()> function) : m_Function(std::move(function)) {}

  void run() override
  {
    // FunctionRunnable is submitted only to dedicated AI-owned pools. Keep these workers below
    // qrenderdoc's UI and replay threads for the whole task.
    QThread::currentThread()->setPriority(QThread::LowPriority);
    if(m_Function)
      m_Function();
  }

private:
  std::function<void()> m_Function;
};

QThreadPool *aiBackgroundThreadPool()
{
  // Thumbnail and attachment decoding must not consume qrenderdoc's shared
  // worker pool. A single low-priority lane keeps UI/replay helpers available.
  static QThreadPool *pool = []() {
    QThreadPool *created = new QThreadPool(QCoreApplication::instance());
    created->setMaxThreadCount(1);
    created->setExpiryTimeout(30000);
    return created;
  }();
  return pool;
}

void persistAIGuiGap(const QByteArray &encoded)
{
  QString local = environmentValue("LOCALAPPDATA");
  if(local.isEmpty())
    local = environmentValue("XDG_DATA_HOME");
  if(local.isEmpty())
    // i18n-ignore: shared per-user runtime directory.
    local = QDir::home().filePath(lit(".local/share"));
  // i18n-ignore: private diagnostic directory and per-process JSONL filename.
  const QString root = QDir(local).filePath(lit("ReplayLab/logs"));
  const QString path = QDir(root).filePath(
      // i18n-ignore: per-process diagnostic JSONL filename.
      lit("gui-responsiveness-%1.jsonl").arg(QCoreApplication::applicationPid()));
  aiBackgroundThreadPool()->start(new FunctionRunnable([root, path, encoded]() {
    if(!QDir().mkpath(root))
      return;
    QFile file(path);
    // This dedicated trace contains no prompts, model output, credentials or file contents.
    // A single background lane serialises appends, and only rare gaps ever enqueue a write.
    const bool full = file.size() + encoded.size() > 1024 * 1024;
    if(file.open(QIODevice::WriteOnly | (full ? QIODevice::Truncate : QIODevice::Append)))
      file.write(encoded);
  }), -1);
}

struct AIConversationSaveWork
{
  QString normalizedPath;
  QJsonDocument document;
  quint64 generation = 0;
};

struct AIConversationSaveQueueState
{
  QMutex mutex;
  QList<AIConversationSaveWork> pending;
  QHash<QString, QPair<quint64, QJsonDocument>> latestDocuments;
  quint64 nextGeneration = 0;
  bool workerRunning = false;
};

std::shared_ptr<AIConversationSaveQueueState> aiConversationSaveQueueState()
{
  static const std::shared_ptr<AIConversationSaveQueueState> state =
      std::make_shared<AIConversationSaveQueueState>();
  return state;
}

QThreadPool *aiConversationSaveThreadPool()
{
  // A single runner drains an explicit FIFO below. This pool is separate from thumbnail decoding
  // so a slow image provider cannot delay an accepted prompt or terminal conversation snapshot.
  static QThreadPool *pool = []() {
    QThreadPool *created = new QThreadPool(QCoreApplication::instance());
    created->setMaxThreadCount(1);
    created->setExpiryTimeout(30000);
    return created;
  }();
  return pool;
}

void drainConversationSaveQueue(const std::shared_ptr<AIConversationSaveQueueState> &state)
{
  while(true)
  {
    AIConversationSaveWork work;
    {
      QMutexLocker locker(&state->mutex);
      if(state->pending.isEmpty())
      {
        state->workerRunning = false;
        return;
      }
      work = state->pending.takeFirst();
    }

    const QByteArray payload = work.document.toJson(QJsonDocument::Compact);
    QString error;
    // Use the normalized path captured on the GUI thread. A later capture rename, deletion or
    // symlink change must not move this generation to a different storage key.
    const bool saved = AIConversationStore::Save(work.normalizedPath, payload, &error);
    {
      QMutexLocker locker(&state->mutex);
      const auto latest = state->latestDocuments.constFind(work.normalizedPath);
      // Keep a failed latest payload available to loadConversations(). A successful commit may be
      // removed only while it is still the newest generation; a newer queued snapshot must remain.
      if(saved && latest != state->latestDocuments.constEnd() &&
         latest.value().first == work.generation)
        state->latestDocuments.remove(work.normalizedPath);
    }
    if(!saved)
      qWarning() << QCoreApplication::translate("AIAssistant", "Could not persist Bread AI conversations:") << error;
  }
}

void queueConversationDocumentSave(const QString &capturePath, const QJsonDocument &document)
{
  const QString normalizedPath = AIConversationStore::NormalizedCapturePath(capturePath);
  if(normalizedPath.isEmpty())
    return;

  const std::shared_ptr<AIConversationSaveQueueState> state = aiConversationSaveQueueState();
  bool startWorker = false;
  {
    QMutexLocker locker(&state->mutex);
    AIConversationSaveWork work;
    work.normalizedPath = normalizedPath;
    work.document = document;
    work.generation = ++state->nextGeneration;

    // Coalesce only work which has not started. Removing the older same-key entry and appending
    // the replacement preserves the global enqueue order across capture rebinds.
    for(int i = state->pending.size() - 1; i >= 0; --i)
      if(state->pending[i].normalizedPath == normalizedPath)
        state->pending.removeAt(i);
    state->pending.push_back(work);
    state->latestDocuments[normalizedPath] = qMakePair(work.generation, document);
    if(!state->workerRunning)
    {
      state->workerRunning = true;
      startWorker = true;
    }
  }

  if(startWorker)
    aiConversationSaveThreadPool()->start(
        new FunctionRunnable([state]() { drainConversationSaveQueue(state); }), -1);
}

bool latestConversationPayload(const QString &capturePath, QByteArray *payload)
{
  if(!payload)
    return false;
  const QString normalizedPath = AIConversationStore::NormalizedCapturePath(capturePath);
  if(normalizedPath.isEmpty())
    return false;
  const std::shared_ptr<AIConversationSaveQueueState> state = aiConversationSaveQueueState();
  QJsonDocument document;
  {
    QMutexLocker locker(&state->mutex);
    const auto latest = state->latestDocuments.constFind(normalizedPath);
    if(latest == state->latestDocuments.constEnd())
      return false;
    document = latest.value().second;
  }
  // This conversion is needed only for the rare close/reopen race. Ordinary UI, Send and terminal
  // paths leave compact JSON generation, base64 and disk I/O entirely on the writer thread.
  *payload = document.toJson(QJsonDocument::Compact);
  return true;
}

bool mimeDataHasAttachmentCandidate(const QMimeData *mimeData)
{
  if(!mimeData)
    return false;
  if(mimeData->hasUrls())
  {
    for(const QUrl &url : mimeData->urls())
      if(url.isLocalFile() && !url.toLocalFile().isEmpty())
        return true;
  }
  if(mimeData->hasImage())
    return true;
  for(const QString &format : mimeData->formats())
    if(validRasterImageMediaType(format))
      return true;
  return false;
}

class AIAttachmentDropFrame : public QFrame
{
public:
  explicit AIAttachmentDropFrame(QWidget *parent = NULL) : QFrame(parent)
  {
    setAcceptDrops(true);
  }

  std::function<bool(const QMimeData *)> insertAttachments;

protected:
  void dragEnterEvent(QDragEnterEvent *event) override
  {
    if(insertAttachments && mimeDataHasAttachmentCandidate(event->mimeData()))
      event->acceptProposedAction();
    else
      QFrame::dragEnterEvent(event);
  }

  void dragMoveEvent(QDragMoveEvent *event) override
  {
    if(insertAttachments && mimeDataHasAttachmentCandidate(event->mimeData()))
      event->acceptProposedAction();
    else
      QFrame::dragMoveEvent(event);
  }

  void dropEvent(QDropEvent *event) override
  {
    if(insertAttachments && insertAttachments(event->mimeData()))
    {
      event->acceptProposedAction();
      return;
    }
    QFrame::dropEvent(event);
  }
};

class AIPromptEdit : public QPlainTextEdit
{
public:
  explicit AIPromptEdit(QWidget *parent = NULL) : QPlainTextEdit(parent) {}

  std::function<void()> send;
  std::function<bool(const QMimeData *)> insertAttachments;
  // Raised right after an "@" is typed, so the panel can offer the Skill/MCP picker.
  std::function<void()> mentionRequested;

protected:
  void insertFromMimeData(const QMimeData *source) override
  {
    if(insertAttachments && insertAttachments(source))
      return;
    QPlainTextEdit::insertFromMimeData(source);
  }

  void keyPressEvent(QKeyEvent *event) override
  {
    if((event->key() == Qt::Key_Return || event->key() == Qt::Key_Enter) &&
       !(event->modifiers() & Qt::ShiftModifier))
    {
      if(send)
        send();
      event->accept();
      return;
    }
    QPlainTextEdit::keyPressEvent(event);
    if(mentionRequested && event->text() == lit("@"))
      mentionRequested();
  }
};

struct ProviderCatalogText
{
  const char *source;
  const char *disambiguation;
};

struct ProviderCatalogTranslation
{
  const char *presetId;
  const char *field;
  ProviderCatalogText text;
};

struct ProviderRuntimeTranslation
{
  const char *messageId;
  ProviderCatalogText text;
};

// ReplayLab's provider catalog is runtime JSON, so lupdate cannot discover it on its own. Keep an
// exact (preset ID, field, English source) registry here: known catalog text is translated, while
// changed or third-party text safely falls back to the backend value instead of receiving a stale
// translation intended for a different message.
static const ProviderCatalogTranslation providerCatalogTranslations[] = {
    {"openai", "title", QT_TRANSLATE_NOOP3("AIAssistant", "OpenAI API", "provider.openai.title")},
    {"anthropic", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Anthropic API", "provider.anthropic.title")},
    {"gemini", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Google Gemini API", "provider.gemini.title")},
    {"vertex", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Google Cloud / Vertex AI", "provider.vertex.title")},
    {"azure-openai", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Azure OpenAI", "provider.azure-openai.title")},
    {"openrouter", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "OpenRouter", "provider.openrouter.title")},
    {"xai", "title", QT_TRANSLATE_NOOP3("AIAssistant", "xAI / Grok", "provider.xai.title")},
    {"mistral", "title", QT_TRANSLATE_NOOP3("AIAssistant", "Mistral AI", "provider.mistral.title")},
    {"groq", "title", QT_TRANSLATE_NOOP3("AIAssistant", "GroqCloud", "provider.groq.title")},
    {"deepseek", "title", QT_TRANSLATE_NOOP3("AIAssistant", "DeepSeek", "provider.deepseek.title")},
    {"qwen", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Alibaba Model Studio / Qwen", "provider.qwen.title")},
    {"qwen-coding-plan", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Alibaba Model Studio Coding Plan",
                        "provider.qwen-coding-plan.title")},
    {"kimi", "title", QT_TRANSLATE_NOOP3("AIAssistant", "Moonshot / Kimi", "provider.kimi.title")},
    {"minimax", "title", QT_TRANSLATE_NOOP3("AIAssistant", "MiniMax", "provider.minimax.title")},
    {"glm", "title", QT_TRANSLATE_NOOP3("AIAssistant", "Zhipu GLM", "provider.glm.title")},
    {"doubao", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Volcengine Ark / Doubao", "provider.doubao.title")},
    {"hunyuan", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Tencent Hunyuan / TokenHub", "provider.hunyuan.title")},
    {"qianfan", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Baidu Qianfan", "provider.qianfan.title")},
    {"siliconflow", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "SiliconFlow", "provider.siliconflow.title")},
    {"ollama", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Ollama (local)", "provider.ollama.title")},
    {"custom-openai", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Custom OpenAI-compatible endpoint",
                        "provider.custom-openai.title")},
    {"codex", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex / ChatGPT subscription", "provider.codex.title")},
    {"copilot-acp", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "GitHub Copilot CLI (ACP)", "provider.copilot-acp.title")},
    {"cursor-acp", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Cursor CLI (ACP)", "provider.cursor-acp.title")},
    {"gemini-acp", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Gemini CLI (ACP)", "provider.gemini-acp.title")},
    {"qwen-acp", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Qwen Code / Coding Plan (ACP)", "provider.qwen-acp.title")},
    {"kimi-acp", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Kimi CLI (ACP)", "provider.kimi-acp.title")},
    {"codebuddy-acp", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Tencent CodeBuddy / Hunyuan Subscription (ACP)",
                        "provider.codebuddy-acp.title")},
    {"custom-acp", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Custom ACP agent", "provider.custom-acp.title")},

    {"anthropic", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Uses an Anthropic API key. To use a Claude Code subscription instead, select the "
         "separate Claude Code ACP preset; it wraps the official Claude Code CLI with the Agent "
         "Client Protocol reference adapter and remains subject to Anthropic's third-party "
         "authentication and usage terms.",
         "provider.anthropic.note")},
    {"azure-openai", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "The profile model is the Azure deployment name. Legacy endpoints require api_version; "
         "Azure v1/Foundry endpoints must omit it.",
         "provider.azure-openai.note")},
    {"groq", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "OpenAI compatibility is partial; run the connection probe for the chosen model.",
         "provider.groq.note")},
    {"qwen", "note",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "Standard API keys and Coding Plan keys use their documented endpoints.",
                        "provider.qwen.note")},
    {"qwen-coding-plan", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "The Coding Plan is a subscription key with a dedicated endpoint, not an OAuth login. "
         "Use the international endpoint as a custom base URL when applicable.",
         "provider.qwen-coding-plan.note")},
    {"minimax", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Supports metered keys and official Token Plan keys; it is not a browser login.",
         "provider.minimax.note")},
    {"custom-openai", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Capabilities are unknown until probed; no feature is inferred from the name.",
         "provider.custom-openai.note")},
    {"copilot-acp", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Uses the official `copilot login --web-flow/--device-code` command, then tests `copilot "
         "--acp`. The ACP handshake may expose model choices; otherwise leave the model blank to "
         "use Copilot's default or enter an exact supported model manually.",
         "provider.copilot-acp.note")},
    {"cursor-acp", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Uses Cursor's official `cursor-agent` executable (also installed as `agent`). The wizard "
         "runs the `login` browser flow for a Cursor subscription, then starts ACP with that "
         "stored login and tests `cursor-agent acp`. Model choices are imported when exposed; "
         "otherwise leave the model blank for Cursor's configured default.",
         "provider.cursor-acp.note")},
    {"gemini-acp", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Gemini authenticates interactively before Bread AI starts `gemini --acp`. Model "
         "choices are used only when the ACP session exposes them; otherwise use the provider "
         "default or enter an exact model supported by your installed CLI.",
         "provider.gemini-acp.note")},
    {"qwen-acp", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Qwen OAuth was discontinued on 2026-04-15. This wizard opens `qwen` for the current "
         "/auth Coding Plan/API-provider setup, then tests `qwen --acp`. ACP model choices are "
         "used when exposed; otherwise use the configured default or enter an exact model.",
         "provider.qwen-acp.note")},
    {"kimi-acp", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Uses the official RFC 8628 `kimi login` device-code flow, then tests `kimi acp`. "
         "Bread AI imports model choices exposed by the ACP session; if none are exposed, leave "
         "the model blank for Kimi's default or enter a configured model alias.",
         "provider.kimi-acp.note")},
    {"codebuddy-acp", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Uses the official CodeBuddy Code CLI and the signed-in CodeBuddy account subscription. "
         "Hunyuan and other model choices are imported only when the ACP session exposes them; "
         "model availability and quota are controlled by the CodeBuddy plan.",
         "provider.codebuddy-acp.note")},
    {"codex", "install_hint",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "Install the official Codex CLI: "
                        "https://developers.openai.com/codex/cli/",
                        "provider.codex.install_hint")},
    {"copilot-acp", "install_hint",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "GitHub Copilot CLI was not found. Install it with `winget install GitHub.Copilot` or "
         "`npm install -g @github/copilot`, then restart qrenderdoc. Official guide: "
         "https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/"
         "install-copilot-cli",
         "provider.copilot-acp.install_hint")},
    {"cursor-acp", "install_hint",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Cursor CLI with ACP support was not found. Install or update the official CLI, add the "
         "`cursor-agent` executable to PATH, then restart qrenderdoc. Official guide: "
         "https://cursor.com/docs/cli/installation",
         "provider.cursor-acp.install_hint")},
    {"gemini-acp", "install_hint",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Gemini CLI was not found. Install it with `npm install -g @google/gemini-cli`, then "
         "restart qrenderdoc. Official guide: https://github.com/google-gemini/gemini-cli/blob/"
         "main/docs/get-started/installation.mdx",
         "provider.gemini-acp.install_hint")},
    {"qwen-acp", "install_hint",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Qwen Code was not found. Install it with `npm install -g "
         "@qwen-code/qwen-code@latest` (or the official Windows installer), then restart "
         "qrenderdoc. Official guide: "
         "https://qwenlm.github.io/qwen-code-docs/en/users/quickstart/",
         "provider.qwen-acp.install_hint")},
    {"kimi-acp", "install_hint",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Kimi Code CLI was not found. Install it with the official Windows script or `npm "
         "install -g @moonshot-ai/kimi-code`, then restart qrenderdoc. Official guide: "
         "https://www.kimi.com/code/docs/en/kimi-code-cli/guides/getting-started.html",
         "provider.kimi-acp.install_hint")},
    {"codebuddy-acp", "install_hint",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "CodeBuddy Code was not found. Install it with `npm install -g "
         "@tencent-ai/codebuddy-code` or the official Windows installer, then restart qrenderdoc. "
         "Official guides: https://www.codebuddy.cn/docs/cli/quickstart and "
         "https://www.codebuddy.cn/docs/cli/acp",
         "provider.codebuddy-acp.install_hint")},

    {"gemini-acp", "subscription_instruction",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Choose Sign in with Google in the Gemini terminal, complete the browser flow, then "
         "exit the terminal after authentication succeeds.",
         "provider.gemini-acp.subscription_instruction")},
    {"qwen-acp", "subscription_instruction",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Type /auth in the Qwen terminal, configure Alibaba ModelStudio Coding Plan or an API "
         "provider, then exit after /doctor reports a usable configuration.",
         "provider.qwen-acp.subscription_instruction")},
    {"codebuddy-acp", "subscription_instruction",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Choose Log in via Chinese Site in the CodeBuddy terminal, complete the browser flow "
         "with the account that owns the subscription, then exit after authentication succeeds.",
         "provider.codebuddy-acp.subscription_instruction")},
    {"claude-code-acp", "title",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Claude Code (ACP subscription)",
         "provider.claude-code-acp.title")},
    {"claude-code-acp", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Connects a Claude subscription through the official Claude Code CLI login, bridged to "
         "ACP by the `@agentclientprotocol/claude-agent-acp` reference adapter (formerly "
         "@zed-industries/claude-code-acp) -- no API key and no reverse-engineered authentication. "
         "The connection wizard runs `claude auth login` in a profile-isolated Claude state "
         "directory; every task launches `claude-agent-acp` with the same directory. Model: pick "
         "one from the list, or add a full model id with the box + Add button (name plus version, "
         "for example `claude-sonnet-5`). Leave it blank to follow your subscription's default; "
         "Bread AI passes the id to Claude Code as ANTHROPIC_MODEL (a model your plan does not "
         "include will be rejected by Claude Code at run time). The adapter supports HTTP/stdio "
         "MCP servers, permission prompts and thinking, which the archived `acp-claude-code` "
         "package did not. Using a subscription through a third-party ACP client must follow "
         "Anthropic's third-party authentication and usage terms.",
         "provider.claude-code-acp.note")},
    {"claude-code-acp", "install_hint",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "The Claude Code ACP bridge is missing. Install Node.js 22+, the official Claude Code "
         "CLI, and `npm install -g @agentclientprotocol/claude-agent-acp` (binary "
         "`claude-agent-acp`), then restart qrenderdoc. The bundled "
         "packaging/setup_claude_code_acp.ps1 installs or updates these dependencies; complete "
         "sign-in from the Save and Connect flow so credentials are stored in this "
         "connection's isolated profile. The archived `acp-claude-code` package is no longer "
         "supported: it ignores MCP servers and never asks for permissions. Adapter: "
         "https://github.com/agentclientprotocol/claude-agent-acp",
         "provider.claude-code-acp.install_hint")},
    {"claude-code-acp", "subscription_instruction",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Authenticate the official Claude Code CLI in the opened terminal: run `claude auth login`, "
         "complete Anthropic's sign-in with the account that owns your Claude subscription, then exit "
         "after it reports success. This terminal already uses this connection's isolated "
         "Claude profile; Bread AI only reuses Claude Code's stored official credentials and never "
         "sees your password or token.",
         "provider.claude-code-acp.subscription_instruction")},
};

// Subscription events are backend protocol data. Translate only stable message IDs; the raw
// English text remains a diagnostic fallback for unknown/third-party messages.
static const ProviderRuntimeTranslation providerRuntimeTranslations[] = {
    {"subscription.claude-account-unauthenticated",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "This Claude connection is not signed in. Use Save and Connect to sign in to its isolated profile.",
                        "subscription.claude-account-unauthenticated")},
    {"subscription.claude-account-signed-in",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "Claude is signed in for this connection; model request access has not been tested.",
                        "subscription.claude-account-signed-in")},
    {"subscription.claude-auth-status-failed",
     QT_TRANSLATE_NOOP3("AIAssistant", "Claude sign-in status could not be verified: %1",
                        "subscription.claude-auth-status-failed")},
    {"subscription.claude-sign-in-test-succeeded",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "Claude sign-in status and ACP session handshake succeeded without sending a prompt.",
                        "subscription.claude-sign-in-test-succeeded")},
    {"claude-auth.skipped",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "Claude sign-in status was not checked because the profile or trusted CLI is unavailable.",
                        "claude-auth.skipped")},
    {"connection.execution-ok",
     QT_TRANSLATE_NOOP3("AIAssistant", "The model generated a reply, called the test MCP tool and completed. The execution receipt was verified.",
                        "connection.execution-ok")},
    {"connection.execution-failed",
     QT_TRANSLATE_NOOP3("AIAssistant", "The model and tool execution check failed: %1", "connection.execution-failed")},
      {"connection.check-running",
       QT_TRANSLATE_NOOP3("AIAssistant", "Checking connection...", "connection.check-running")},
    {"connection.portable-ok",
     QT_TRANSLATE_NOOP3("AIAssistant", "%1 bundled plugins are installed with portable tool dependencies.", "connection.portable-ok")},
    {"connection.portable-missing",
     QT_TRANSLATE_NOOP3("AIAssistant", "Portable tool dependencies are missing or incomplete.", "connection.portable-missing")},
    {"connection.portable-execution-ok",
     QT_TRANSLATE_NOOP3("AIAssistant", "The bundled DOCX, XLSX, PPTX and PDF tools executed successfully.", "connection.portable-execution-ok")},
    {"connection.host-execution-ok",
     QT_TRANSLATE_NOOP3("AIAssistant", "The Bread Host initialized and executed its read-only runtime status tool.", "connection.host-execution-ok")},
    {"subscription.cli-detected",
     QT_TRANSLATE_NOOP3(
         "AIAssistant", "%1 CLI detected; sign-in state is checked only after approval.",
         "subscription.cli-detected")},
    {"subscription.cli-missing",
     QT_TRANSLATE_NOOP3("AIAssistant", "Required provider CLI was not found: %1\n%2",
                        "subscription.cli-missing")},
    {"subscription.waiting-for-containment",
     QT_TRANSLATE_NOOP3(
         "AIAssistant", "Waiting for qrenderdoc process containment before starting the provider.",
         "subscription.waiting-for-containment")},
    {"subscription.sign-in-started",
     QT_TRANSLATE_NOOP3(
         "AIAssistant", "Provider sign-in started inside the qrenderdoc process job.",
         "subscription.sign-in-started")},
    {"subscription.test-started",
     QT_TRANSLATE_NOOP3(
         "AIAssistant", "Running a bounded no-generation provider connection test.",
         "subscription.test-started")},
    {"subscription.browser-waiting",
     QT_TRANSLATE_NOOP3(
         "AIAssistant", "Waiting for %1 browser/device authorization.",
         "subscription.browser-waiting")},
    {"subscription.browser-complete",
     QT_TRANSLATE_NOOP3("AIAssistant", "Complete provider authorization in the browser.",
                        "subscription.browser-complete")},
    {"subscription.cancelled-before-start",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "Subscription operation cancelled before provider startup.",
                        "subscription.cancelled-before-start")},
    {"subscription.start-gate-closed",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Subscription start gate closed before 'start'; no provider process was created.",
         "subscription.start-gate-closed")},
    {"subscription.custom-login-skipped",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Custom ACP has no guessed login command; running only the configured handshake test.",
         "subscription.custom-login-skipped")},
    {"subscription.acp-test-models",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "ACP initialize/session handshake succeeded without sending a prompt; the session "
         "exposed %1 model choice(s).",
         "subscription.acp-test-models")},
    {"subscription.acp-test-no-models",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "ACP initialize/session handshake succeeded without sending a prompt. This Agent did "
         "not expose a standard model list; leave the model unset to use its configured default, "
         "or enter an exact model only if that CLI supports ACP model selection.",
         "subscription.acp-test-no-models")},
    {"subscription.acp-login-completed",
     QT_TRANSLATE_NOOP3("AIAssistant", "%1 login command completed; testing ACP now.",
                        "subscription.acp-login-completed")},
    {"subscription.terminal-opened",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "Complete authentication in the %1 terminal, then exit it.",
                        "subscription.terminal-opened")},
    {"subscription.terminal-login-completed",
     QT_TRANSLATE_NOOP3("AIAssistant", "%1 terminal exited; testing ACP now.",
                        "subscription.terminal-login-completed")},
    {"subscription.codex-signed-out",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex is signed out.",
                        "subscription.codex-signed-out")},
    {"subscription.codex-account-ready",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex account and model catalog are available.",
                        "subscription.codex-account-ready")},
    {"subscription.codex-browser-complete",
     QT_TRANSLATE_NOOP3("AIAssistant", "Complete Codex sign-in in the browser.",
                        "subscription.codex-browser-complete")},
    {"subscription.codex-login-completed",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex login completed.",
                        "subscription.codex-login-completed")},
    {"subscription.codex-login-failed",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex login failed.",
                        "subscription.codex-login-failed")},
    {"subscription.codex-login-failed-detail",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex login failed: %1",
                        "subscription.codex-login-failed-detail")},
    {"subscription.codex-account-unauthenticated",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex account is not authenticated.",
                        "subscription.codex-account-unauthenticated")},
    {"subscription.codex-sign-in-test-succeeded",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex sign-in and model catalog test succeeded.",
                        "subscription.codex-sign-in-test-succeeded")},
};

// ReplayLab task events carry stable IDs for every normal activity label. Dynamic Skill names,
// tool identifiers, and public progress notes are inserted only as bounded arguments. Unknown
// provider IDs keep their diagnostic fallback.
static const ProviderRuntimeTranslation taskRuntimeTranslations[] = {
    {"task.frame-flow.waiting-for-arguments",
     QT_TRANSLATE_NOOP3("AIAssistant", "Waiting for the model to submit frame diagram arguments",
                        "task.frame-flow.waiting-for-arguments")},
    {"task.capabilities.codex-configured",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex configured capabilities",
                        "task.capabilities.codex-configured")},
    {"task.capabilities.inherited",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "User-configured integrations and permissions are inherited. Only connected services are "
         "callable; new approval requests are not automatically accepted.",
         "task.capabilities.inherited")},
    {"task.provider.retry-capacity",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "Model capacity is temporarily unavailable. Continuing this session in %1 "
                        "seconds (retry %2 of %3).",
                        "task.provider.retry-capacity")},
    {"task.provider.retry-session",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "The provider's account lookup timed out. Retrying in %1 seconds (%2 of %3).",
         "task.provider.retry-session")},
    {"task.provider.session-ready",
     QT_TRANSLATE_NOOP3("AIAssistant", "Provider connection restored. Continuing the request.",
                        "task.provider.session-ready")},
    {"task.error.provider-capacity",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "The selected model is temporarily at capacity. Retry later or choose "
                        "another model; the task is unfinished.",
                        "task.error.provider-capacity")},
    {"doctor.context-window-unknown",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "The context window for %1 is not published. Requests can still be sent; "
                        "the server checks its actual context limit.",
                        "doctor.context-window-unknown")},
    {"task.error.provider-rate-limit",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "The provider limited the request rate. Wait before continuing the unfinished task.",
         "task.error.provider-rate-limit")},
    {"task.error.provider-quota",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "The provider reports an exhausted usage or billing limit. Check the "
                        "account quota before retrying.",
                        "task.error.provider-quota")},
    {"task.error.provider-authentication",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "The provider rejected authentication. Check this connection's sign-in or API credential.",
         "task.error.provider-authentication")},
    {"task.error.provider-permission",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "The provider denied access. Check account and model permissions for this connection.",
         "task.error.provider-permission")},
    {"task.error.provider-model-unavailable",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "The configured model is unavailable. Refresh the model list and check the "
                        "selected model.",
                        "task.error.provider-model-unavailable")},
    {"task.error.provider-timeout",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "The provider request timed out. Check the connection and continue from "
                        "the saved task state.",
                        "task.error.provider-timeout")},
    {"task.error.provider-transport",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "The provider connection was interrupted. Check network and proxy settings "
                        "before continuing.",
                        "task.error.provider-transport")},
    {"task.error.provider-unavailable",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "The provider service is temporarily unavailable. Retry the unfinished task later.",
         "task.error.provider-unavailable")},
    {"task.error.provider-configuration",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "The provider rejected the connection or request configuration. Review the "
                        "diagnostic details before retrying.",
                        "task.error.provider-configuration")},
    {"task.error.provider-context",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "The request exceeds the model context limit. Continue with a smaller "
                        "context while preserving saved work.",
                        "task.error.provider-context")},
    {"task.error.provider-failed",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "The AI task failed. Review the diagnostic details; completion has not been confirmed.",
         "task.error.provider-failed")},
    {"task.error.provider-policy",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "The provider declined this request under its policy. Review the "
                        "diagnostic details before changing the request.",
                        "task.error.provider-policy")},
    {"task.owner-exited",
     QT_TRANSLATE_NOOP3("AIAssistant", "The application exited; the unfinished task was cancelled.",
                        "task.owner-exited")},
    {"task.queued", QT_TRANSLATE_NOOP3("AIAssistant", "Queued", "task.queued")},
    {"task.main.started",
     QT_TRANSLATE_NOOP3("AIAssistant", "Main Agent is analysing the request", "task.main.started")},
    {"task.shader-review.started",
     QT_TRANSLATE_NOOP3("AIAssistant", "Reviewing organized Shader source",
                        "task.shader-review.started")},
    {"task.self-review.started",
     QT_TRANSLATE_NOOP3("AIAssistant", "Reviewing task completion", "task.self-review.started")},
    {"task.main.completed",
     QT_TRANSLATE_NOOP3("AIAssistant", "Main Agent finished this turn", "task.main.completed")},
    {"task.child.started",
     QT_TRANSLATE_NOOP3("AIAssistant", "Child Agent started: %1", "task.child.started")},
    {"task.child.completed",
     QT_TRANSLATE_NOOP3("AIAssistant", "Child Agent completed: %1", "task.child.completed")},
    {"task.child.failed",
     QT_TRANSLATE_NOOP3("AIAssistant", "Child Agent failed: %1", "task.child.failed")},
    {"task.detail.public-summary",
     QT_TRANSLATE_NOOP3("AIAssistant", "%1", "task.detail.public-summary")},
};

QString localizeProviderCatalogText(const QString &presetId, const QString &field,
                                    const QString &source, bool *matched = NULL)
{
  if(matched)
    *matched = false;
  if(presetId.isEmpty() || source.isEmpty())
    return source;

  for(const ProviderCatalogTranslation &entry : providerCatalogTranslations)
  {
    if(presetId == QLatin1String(entry.presetId) && field == QLatin1String(entry.field) &&
       source == QLatin1String(entry.text.source))
    {
      if(matched)
        *matched = true;
      return QCoreApplication::translate("AIAssistant", entry.text.source,
                                         entry.text.disambiguation);
    }
  }

  return source;
}

QString localizeProviderCatalogField(const QJsonObject &preset, const char *field)
{
  // i18n-ignore: ReplayLab provider-catalog JSON field name used only for lookup.
  const QString presetIdKey = lit("id");
  const QString fieldName = QString::fromLatin1(field);
  return localizeProviderCatalogText(preset.value(presetIdKey).toString(), fieldName,
                                     preset.value(fieldName).toString());
}

QString localizeProviderRuntimeText(const QString &presetId, const QString &source)
{
  if(presetId.isEmpty() || source.isEmpty())
    return source;

  for(const ProviderCatalogTranslation &entry : providerCatalogTranslations)
  {
    if(presetId == QLatin1String(entry.presetId) && source == QLatin1String(entry.text.source))
      return QCoreApplication::translate("AIAssistant", entry.text.source,
                                         entry.text.disambiguation);
  }

  return source;
}

QString localizeProviderRuntimeMessage(const QString &presetId, const QString &messageId,
                                       const QJsonArray &arguments, const QString &fallback)
{
  // The terminal login event deliberately carries provider-specific instructions such as
  // Gemini's browser choice or Qwen's /auth and /doctor steps. Prefer that exact reviewed catalog
  // source over the generic stable event text, without translating unknown backend prose.
  // i18n-ignore: stable ReplayLab runtime-message protocol ID.
  if(messageId == QLatin1String("subscription.terminal-opened"))
  {
    bool matched = false;
    const QString instruction = localizeProviderCatalogText(
        // i18n-ignore: provider-catalog field identifier used only for localized lookup.
        presetId, lit("subscription_instruction"), fallback, &matched);
    if(matched)
      return instruction;
  }
  if(!messageId.isEmpty())
  {
    for(const ProviderRuntimeTranslation &entry : providerRuntimeTranslations)
    {
      if(messageId != QLatin1String(entry.messageId))
        continue;
      QString result = QCoreApplication::translate("AIAssistant", entry.text.source,
                                                   entry.text.disambiguation);
      const int count = qMin(arguments.size(), 8);
      for(int i = 0; i < count; ++i)
      {
        QString value = arguments.at(i).toVariant().toString().left(512);
        // i18n-ignore: stable ReplayLab runtime-message protocol ID.
        if(messageId == QLatin1String("subscription.cli-missing") && i == 1)
          // i18n-ignore: provider-catalog field identifier used only for localized lookup.
          value = localizeProviderCatalogText(presetId, lit("install_hint"), value);
        result = result.arg(value);
      }
      return result;
    }
  }
  return localizeProviderRuntimeText(presetId, fallback);
}

QString localizeTaskRuntimeMessage(const QString &messageId, const QJsonArray &arguments,
                                   const QString &fallback)
{
  if(!messageId.isEmpty())
  {
    for(const ProviderRuntimeTranslation &entry : taskRuntimeTranslations)
    {
      if(messageId != QLatin1String(entry.messageId))
        continue;
      QString result = QCoreApplication::translate("AIAssistant", entry.text.source,
                                                   entry.text.disambiguation);
      const int count = qMin(arguments.size(), 8);
      for(int i = 0; i < count; ++i)
        result = result.arg(arguments.at(i).toVariant().toString().left(1000));
      return result;
    }
  }
  return fallback;
}

QString connectionDisplayName(const QJsonObject &row)
{
  const QString title = row.value(lit("title")).toString().trimmed();
  return title.isEmpty() ? row.value(lit("id")).toString() : title;
}

bool supportedQRenderDocConnection(const QJsonObject &object)
{
  // i18n-ignore: ReplayLab provider/profile JSON field name.
  QString presetId = object.value(lit("preset")).toString();
  if(presetId.isEmpty())
    // i18n-ignore: ReplayLab provider-catalog JSON field name.
    presetId = object.value(lit("id")).toString();
  // i18n-ignore: ReplayLab provider kind and preset identifiers used only for filtering.
  const QString kind = object.value(lit("kind")).toString();
  // i18n-ignore: ReplayLab provider protocol field and identifier used only for filtering.
  const QString protocol = object.value(lit("protocol")).toString();
  const QString modelKind = lit("model");    // i18n-ignore: provider kind identifier.
  const QString agentKind = lit("agent");    // i18n-ignore: provider kind identifier.
  const QString codexPreset = lit("codex");    // i18n-ignore: provider preset identifier.
  const QString codexProtocol =
      lit("codex-app-server");    // i18n-ignore: provider protocol identifier.
  const QString acpProtocol = lit("acp");    // i18n-ignore: provider protocol identifier.
  return kind == modelKind ||
         (kind == agentKind &&
          ((presetId == codexPreset && protocol == codexProtocol) || protocol == acpProtocol));
}

QString internalConnectionId()
{
  QString uuid = QUuid::createUuid().toString();
  uuid.remove(QLatin1Char('{'));
  uuid.remove(QLatin1Char('}'));
  return lit("connection-") + uuid;
}

// QLabel's ordinary word wrapping still keeps an unbroken path, hash, or API token on one line.
// Task summaries can contain all three, so lay them out with a safe anywhere fallback.
class AITaskProgressLabel : public QLabel
{
public:
  explicit AITaskProgressLabel(const QString &text, QWidget *parent = NULL) : QLabel(text, parent)
  {
    setTextFormat(Qt::PlainText);
    setWordWrap(true);
    setMinimumWidth(0);
    setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Minimum);
  }

  bool hasHeightForWidth() const override { return true; }

  int heightForWidth(int width) const override
  {
    QTextLayout layout(text(), font());
    return qMax(fontMetrics().height(), int(std::ceil(layoutText(layout, width))));
  }

protected:
  void paintEvent(QPaintEvent *) override
  {
    const QRect textRect = contentsRect();
    QTextLayout layout(text(), font());
    layoutText(layout, textRect.width());

    QPainter painter(this);
    const QPalette::ColorGroup colorGroup = isEnabled() ? QPalette::Active : QPalette::Disabled;
    painter.setPen(palette().color(colorGroup, foregroundRole()));
    layout.draw(&painter, QPointF(textRect.left(), textRect.top()));
  }

private:
  qreal layoutText(QTextLayout &layout, int width) const
  {
    QTextOption option;
    option.setWrapMode(QTextOption::WrapAtWordBoundaryOrAnywhere);
    option.setTextDirection(layoutDirection());
    option.setAlignment(alignment() & Qt::AlignHorizontal_Mask);
    layout.setTextOption(option);

    const qreal lineWidth = qMax(1, width);
    qreal height = 0.0;
    layout.beginLayout();
    while(true)
    {
      QTextLine line = layout.createLine();
      if(!line.isValid())
        break;
      line.setLineWidth(lineWidth);
      line.setPosition(QPointF(0.0, height));
      height += line.height();
    }
    layout.endLayout();
    return height;
  }
};

// QMenu's ordinary QAction rows are intrinsically single-line. A fixed-width widget action keeps
// the popup bounded to the composer, lets long task summaries wrap, and gives each projected state
// a shape as well as a translated text label. The running ring follows the same 90 ms cadence as
// the conversation-list spinner.
class AITaskProgressRow : public QWidget
{
public:
  enum class State
  {
    Completed,
    Failed,
    Waiting,
    Running,
  };

  AITaskProgressRow(const QString &text, State state, int rowWidth, QWidget *parent = NULL)
      : QWidget(parent), m_Text(text), m_State(state), m_RowWidth(rowWidth)
  {
    setFixedWidth(m_RowWidth);
    setSizePolicy(QSizePolicy::Fixed, QSizePolicy::Minimum);
    setAccessibleName(m_Text);
    setToolTip(m_Text);

    QHBoxLayout *layout = new QHBoxLayout(this);
    layout->setContentsMargins(34, 6, 12, 6);
    layout->setSpacing(0);
    m_Label = new AITaskProgressLabel(m_Text, this);
    layout->addWidget(m_Label);

    if(m_State == State::Running)
      ensureSpinnerTimer();
  }

  void updateProgress(const QString &text, State state, int rowWidth)
  {
    bool geometryChanged = false;
    bool stateChanged = false;
    if(m_Text != text)
    {
      m_Text = text;
      m_Label->setText(m_Text);
      setAccessibleName(m_Text);
      setToolTip(m_Text);
      geometryChanged = true;
    }
    if(m_RowWidth != rowWidth)
    {
      m_RowWidth = rowWidth;
      setFixedWidth(m_RowWidth);
      geometryChanged = true;
    }
    if(m_State != state)
    {
      m_State = state;
      stateChanged = true;
      if(m_State == State::Running)
      {
        ensureSpinnerTimer();
        if(isVisible())
          m_SpinnerTimer->start();
      }
      else if(m_SpinnerTimer)
      {
        m_SpinnerTimer->stop();
      }
    }
    if(geometryChanged)
    {
      m_Label->updateGeometry();
      updateGeometry();
    }
    if(geometryChanged || stateChanged)
      update();
  }

  QSize sizeHint() const override
  {
    const int textWidth = qMax(1, m_RowWidth - 34 - 12);
    return QSize(m_RowWidth, qMax(30, m_Label->heightForWidth(textWidth) + 12));
  }

protected:
  void paintEvent(QPaintEvent *event) override
  {
    QWidget::paintEvent(event);

    QPainter painter(this);
    painter.setRenderHint(QPainter::Antialiasing, true);
    const QPointF center(18.0, 15.0);
    const QRectF circle(center.x() - 6.0, center.y() - 6.0, 12.0, 12.0);

    if(m_State == State::Completed)
    {
      painter.setPen(Qt::NoPen);
      painter.setBrush(diagnosticColor(this, DiagnosticLevel::Success));
      painter.drawEllipse(circle);
      painter.setBrush(Qt::NoBrush);
      painter.setPen(QPen(palette().color(QPalette::Base), 1.7, Qt::SolidLine, Qt::RoundCap,
                          Qt::RoundJoin));
      painter.drawLine(QPointF(center.x() - 3.0, center.y()),
                       QPointF(center.x() - 0.8, center.y() + 2.4));
      painter.drawLine(QPointF(center.x() - 0.8, center.y() + 2.4),
                       QPointF(center.x() + 3.6, center.y() - 2.7));
    }
    else if(m_State == State::Failed)
    {
      painter.setPen(Qt::NoPen);
      painter.setBrush(diagnosticColor(this, DiagnosticLevel::Error));
      painter.drawEllipse(circle);
      painter.setBrush(Qt::NoBrush);
      painter.setPen(
          QPen(palette().color(QPalette::Base), 1.6, Qt::SolidLine, Qt::RoundCap));
      painter.drawLine(QPointF(center.x() - 2.2, center.y() - 2.2),
                       QPointF(center.x() + 2.2, center.y() + 2.2));
      painter.drawLine(QPointF(center.x() + 2.2, center.y() - 2.2),
                       QPointF(center.x() - 2.2, center.y() + 2.2));
    }
    else if(m_State == State::Waiting)
    {
      QColor pending = palette().color(QPalette::Disabled, QPalette::Text);
      pending.setAlpha(150);
      painter.setPen(QPen(pending, 1.4));
      pending.setAlpha(55);
      painter.setBrush(pending);
      painter.drawEllipse(circle);
    }
    else
    {
      QColor track = palette().color(QPalette::Mid);
      track.setAlpha(135);
      painter.setPen(QPen(track, 1.6, Qt::SolidLine, Qt::RoundCap));
      painter.setBrush(Qt::NoBrush);
      painter.drawArc(circle, 0, 360 * 16);

      QColor progress = palette().color(QPalette::Highlight);
      if(progress.alpha() == 0)
        progress = QColor(54, 132, 255);
      painter.setPen(QPen(progress, 1.8, Qt::SolidLine, Qt::RoundCap));
      // The projection is refreshed while streaming. Deriving the phase from wall time keeps the
      // spinner continuous across both in-place updates and the rare structural row replacement.
      const qint64 spinnerTick = QDateTime::currentMSecsSinceEpoch() / 90;
      const int spinnerAngle = int((spinnerTick * 338) % 360);
      painter.drawArc(circle, spinnerAngle * 16, 248 * 16);
    }
  }

  void showEvent(QShowEvent *event) override
  {
    QWidget::showEvent(event);
    if(m_SpinnerTimer)
      m_SpinnerTimer->start();
  }

  void hideEvent(QHideEvent *event) override
  {
    if(m_SpinnerTimer)
      m_SpinnerTimer->stop();
    QWidget::hideEvent(event);
  }

private:
  void ensureSpinnerTimer()
  {
    if(m_SpinnerTimer)
      return;
    m_SpinnerTimer = new QTimer(this);
    m_SpinnerTimer->setInterval(90);
    QObject::connect(m_SpinnerTimer, &QTimer::timeout, this, [this]() { update(); });
  }

  QString m_Text;
  State m_State = State::Waiting;
  int m_RowWidth = 320;
  AITaskProgressLabel *m_Label = NULL;
  QTimer *m_SpinnerTimer = NULL;
};

// The plan control is deliberately a text-only button. QToolButton's native menu handling keeps
// the popup keyboard navigable, while the hover hook makes the same summary available without a
// click. Keyboard focus alone does not open the popup or interrupt normal Tab navigation. The menu
// is updated from host-clean activity metadata by AIAssistant::updateTaskProgressUI().
class AITaskProgressButton : public QToolButton
{
public:
  explicit AITaskProgressButton(QWidget *parent = NULL) : QToolButton(parent) {}

protected:
  void enterEvent(QEvent *event) override
  {
    QToolButton::enterEvent(event);
    showProgressPopup();
  }

private:
  void showProgressPopup()
  {
    QWidget *topLevel = window();
    if(QApplication::applicationState() != Qt::ApplicationActive ||
       (topLevel && !topLevel->isActiveWindow()) || !menu() || menu()->actions().isEmpty() ||
       menu()->isVisible())
      return;

    const QSize popupSize = menu()->sizeHint();
    QPoint popupPosition = mapToGlobal(
        QPoint((width() - popupSize.width()) / 2, -popupSize.height() - 4));
    // A short composer near the top edge still gets a useful popup instead of an off-screen one.
    if(popupPosition.y() < 0)
      popupPosition = mapToGlobal(QPoint((width() - popupSize.width()) / 2, height() + 4));
    menu()->popup(popupPosition);
  }
};
}

AIAssistant::AIAssistant(CaptureContext &ctx, QWidget *parent) : QFrame(parent), m_Ctx(ctx)
{
  setWindowTitle(tr("Bread AI"));
  // The dock must be able to share a screen with the capture and event panels.
  setMinimumSize(QSize(320, 240));

  m_AgentTraceStore = new AIAgentTraceStore();
  m_AgentMonitor = new AIAgentMonitorWindow(*m_AgentTraceStore, this);
  m_AgentMonitor->SetTaskEventRoot(taskEventRoot());

  QVBoxLayout *root = new QVBoxLayout(this);
  root->setContentsMargins(0, 0, 0, 0);
  root->setSpacing(0);

  QWidget *mainPanel = new QWidget(this);
  root->addWidget(mainPanel);
  QPalette mainPalette = QApplication::palette();
  mainPalette.setColor(QPalette::Window, mainPalette.color(QPalette::Base));
  mainPanel->setPalette(mainPalette);
  mainPanel->setAutoFillBackground(true);
  QVBoxLayout *mainLayout = new QVBoxLayout(mainPanel);
  mainLayout->setContentsMargins(7, 5, 7, 6);
  mainLayout->setSpacing(4);

  QFrame *conversationBar = new QFrame(mainPanel);
  conversationBar->setFrameShape(QFrame::NoFrame);
  // i18n-ignore: QObject name is an internal stylesheet selector, not user-visible text.
  conversationBar->setObjectName(lit("aiConversationBar"));
  conversationBar->setStyleSheet(
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QFrame#aiConversationBar { border: none; border-bottom: 1px solid palette(mid); }"));
  QHBoxLayout *conversationBarLayout = new QHBoxLayout(conversationBar);
  conversationBarLayout->setContentsMargins(0, 0, 0, 3);
  conversationBarLayout->setSpacing(3);
  m_ConversationList = new AIConversationListView(conversationBar);
  conversationBarLayout->addWidget(m_ConversationList, 1);
  QPushButton *newConversation = new QPushButton(conversationBar);
  newConversation->setFlat(true);
  newConversation->setFixedSize(30, 30);
  newConversation->setIcon(assistantActionIcon(newConversation, AssistantActionIcon::Add));
  newConversation->setToolTip(tr("New conversation"));
  newConversation->setAccessibleName(tr("New conversation"));
  conversationBarLayout->addWidget(newConversation, 0, Qt::AlignTop);
  mainLayout->addWidget(conversationBar);

  QHBoxLayout *connectionRow = new QHBoxLayout();
  m_Status = new QLabel(this);
  setDiagnosticStatus(m_Status, DiagnosticLevel::Info, tr("Starting..."));
  m_Status->setMinimumWidth(110);
  m_Status->setWordWrap(true);
  m_Status->setTextInteractionFlags(Qt::TextSelectableByMouse |
                                    Qt::TextSelectableByKeyboard);
  m_Status->setFocusPolicy(Qt::StrongFocus);
  connectionRow->addWidget(m_Status);
  connectionRow->addStretch(1);
  QToolButton *agentMonitor = new QToolButton(this);
  agentMonitor->setAutoRaise(true);
  agentMonitor->setFixedSize(30, 30);
  agentMonitor->setIcon(Icons::timeline_marker());
  agentMonitor->setToolTip(tr("Agent Monitor"));
  agentMonitor->setAccessibleName(tr("Agent Monitor"));
  connectionRow->addWidget(agentMonitor);
  QToolButton *connections = new QToolButton(this);
  connections->setAutoRaise(true);
  connections->setFixedSize(30, 30);
  connections->setIcon(Icons::cog());
  connections->setToolTip(tr("AI Settings"));
  connections->setAccessibleName(tr("AI Settings"));
  connectionRow->addWidget(connections);
  mainLayout->addLayout(connectionRow);

  m_Context = new QLabel(this);
  m_Context->setWordWrap(true);
  m_Context->setTextInteractionFlags(Qt::TextSelectableByMouse |
                                     Qt::TextSelectableByKeyboard);
  m_Context->setFocusPolicy(Qt::StrongFocus);
  mainLayout->addWidget(m_Context);

  QHBoxLayout *skills = new QHBoxLayout();
  QPushButton *explain = new QPushButton(tr("Organize Frame Rendering Flow"), this);
  QPushButton *shader = new QPushButton(tr("Organize Current Shader"), this);
  skills->addWidget(explain);
  skills->addWidget(shader);
  skills->addStretch(1);
  mainLayout->addLayout(skills);

  m_Transcript = new AIConversationView(mainPanel);
  m_Transcript->setLinkHandler([this](const QUrl &url) { handleTranscriptLink(url); });
  m_Transcript->setVisualLinkHandler([this](const QUrl &url, const QJsonObject &capture) {
    bool matches = false;
    if(m_Ctx.IsCaptureLoaded())
    {
      const QFileInfo current(QString::fromUtf8(m_Ctx.GetCaptureFilename().c_str()));
      const QFileInfo source(capture.value(QLatin1String("path")).toString());
      if(capture.value(QLatin1String("local")).toBool() && source.exists() && current.exists())
        matches = current.canonicalFilePath().compare(source.canonicalFilePath(),
                                                      Qt::CaseInsensitive) == 0 &&
                  current.size() == qint64(capture.value(QLatin1String("size")).toDouble(-1)) &&
                  current.lastModified().toMSecsSinceEpoch() ==
                      qint64(capture.value(QLatin1String("mtimeMs")).toDouble(-1));
      else
      {
        QString session;
        matches = findBridgeBinding(NULL, &session) && !session.isEmpty() &&
                  session == capture.value(QLatin1String("captureSessionId")).toString();
      }
    }
    if(matches)
      handleTranscriptLink(url);
    else
      appendSystem(tr("Open the source capture to use this visual answer's navigation links."), true);
  });
  m_Transcript->setActivityToggleHandler([this](const QString &messageId, bool expanded) {
    ConversationThread *thread = conversation();
    if(!thread)
      return;
    for(AIChatMessage &item : thread->messages)
    {
      if(item.id == messageId)
      {
        item.activityExpanded = expanded;
        scheduleConversationSave();
        break;
      }
    }
  });
  m_Transcript->setSelectionAttachmentHandler([this](const QString &text) {
    AddTextAttachment(tr("Conversation selection"), text,
                      lit("renderdoc-ai-conversation"));    // i18n-ignore: attachment source ID
  });
  mainLayout->addWidget(m_Transcript, 1);

  AIAttachmentDropFrame *composer = new AIAttachmentDropFrame(mainPanel);
  composer->setFrameShape(QFrame::StyledPanel);
  QVBoxLayout *composerLayout = new QVBoxLayout(composer);
  composerLayout->setContentsMargins(8, 7, 8, 7);
  composerLayout->setSpacing(5);
  m_PendingChats = new AIPendingChatView(composer);
  m_PendingChats->setAdjustDirectionHandler(
      [this](const QString &taskId) { adjustQueuedChat(taskId); });
  m_PendingChats->setEditHandler([this](const QString &taskId) { editQueuedChat(taskId); });
  m_PendingChats->setDeleteHandler(
      [this](const QString &taskId) { deleteQueuedChat(taskId); });
  composerLayout->addWidget(m_PendingChats);

  m_ClarificationPanel = new QFrame(composer);
  m_ClarificationPanel->setFrameShape(QFrame::StyledPanel);
  // i18n-ignore: QObject name is an internal stylesheet selector, not user-visible text.
  m_ClarificationPanel->setObjectName(lit("aiClarificationPanel"));
  m_ClarificationPanel->setStyleSheet(
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QFrame#aiClarificationPanel { border: 1px solid palette(mid); "
          "border-radius: 6px; background: palette(alternate-base); }"));
  QVBoxLayout *clarificationLayout = new QVBoxLayout(m_ClarificationPanel);
  clarificationLayout->setContentsMargins(8, 7, 8, 7);
  clarificationLayout->setSpacing(5);
  m_ClarificationQuestion = new QLabel(m_ClarificationPanel);
  m_ClarificationQuestion->setWordWrap(true);
  m_ClarificationQuestion->setTextInteractionFlags(Qt::TextSelectableByMouse |
                                                    Qt::TextSelectableByKeyboard);
  m_ClarificationQuestion->setFocusPolicy(Qt::StrongFocus);
  clarificationLayout->addWidget(m_ClarificationQuestion);
  m_ClarificationOptions = new QWidget(m_ClarificationPanel);
  m_ClarificationOptionsLayout = new QVBoxLayout(m_ClarificationOptions);
  m_ClarificationOptionsLayout->setContentsMargins(0, 0, 0, 0);
  m_ClarificationOptionsLayout->setSpacing(3);
  clarificationLayout->addWidget(m_ClarificationOptions);
  QHBoxLayout *clarificationSubmitRow = new QHBoxLayout();
  clarificationSubmitRow->setContentsMargins(0, 0, 0, 0);
  clarificationSubmitRow->addStretch(1);
  m_ClarificationSubmit = new QPushButton(tr("Continue"), m_ClarificationPanel);
  m_ClarificationSubmit->setEnabled(false);
  clarificationSubmitRow->addWidget(m_ClarificationSubmit);
  clarificationLayout->addLayout(clarificationSubmitRow);
  m_ClarificationPanel->setVisible(false);
  composerLayout->addWidget(m_ClarificationPanel);

  m_AttachmentScroll = new QScrollArea(composer);
  m_AttachmentScroll->setFrameShape(QFrame::NoFrame);
  m_AttachmentScroll->setWidgetResizable(true);
  m_AttachmentScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);
  m_AttachmentScroll->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
  m_AttachmentScroll->setMaximumHeight(84);
  m_AttachmentStrip = new QWidget(m_AttachmentScroll);
  m_AttachmentLayout = new QHBoxLayout(m_AttachmentStrip);
  m_AttachmentLayout->setContentsMargins(0, 0, 0, 0);
  m_AttachmentLayout->setSpacing(5);
  m_AttachmentLayout->addStretch(1);
  m_AttachmentScroll->setWidget(m_AttachmentStrip);
  m_AttachmentScroll->hide();
  composerLayout->addWidget(m_AttachmentScroll);

  m_TaskProgress = new AITaskProgressButton(composer);
  m_TaskProgress->setToolButtonStyle(Qt::ToolButtonTextOnly);
  m_TaskProgress->setPopupMode(QToolButton::InstantPopup);
  m_TaskProgress->setFocusPolicy(Qt::StrongFocus);
  m_TaskProgress->setCursor(Qt::PointingHandCursor);
  m_TaskProgress->setAccessibleName(tr("Task progress"));
  m_TaskProgress->setAccessibleDescription(
      tr("Show the steps and status of the active AI task."));
  m_TaskProgress->setStyleSheet(
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QToolButton { border: 1px solid palette(mid); border-radius: 12px; "
          "padding: 3px 12px; background: palette(alternate-base); } "
          "QToolButton:hover, QToolButton:focus { background: palette(midlight); }"));
  m_TaskProgressMenu = new QMenu(m_TaskProgress);
  m_TaskProgressMenu->setStyleSheet(
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QMenu { border: 1px solid palette(mid); border-radius: 8px; "
          "padding: 5px; background: palette(window); } "
          "QMenu::item { padding: 5px 12px; } "
          "QMenu::item:selected { background: palette(highlight); "
          "color: palette(highlighted-text); }"));
  m_TaskProgress->setMenu(m_TaskProgressMenu);
  connect(qApp, &QGuiApplication::applicationStateChanged, this,
          [this](Qt::ApplicationState state) {
            // QMenu is a native top-level popup. Explicitly close it on deactivation so Windows
            // cannot leave the progress surface above whichever application the user switches to.
            if(state != Qt::ApplicationActive && m_TaskProgressMenu)
              m_TaskProgressMenu->close();
          });
  m_TaskProgress->hide();
  composerLayout->addWidget(m_TaskProgress, 0, Qt::AlignHCenter);

  AIPromptEdit *promptEdit = new AIPromptEdit(composer);
  m_Prompt = promptEdit;
  m_Prompt->setMaximumBlockCount(200);
  m_Prompt->setMaximumHeight(100);
  m_Prompt->setFrameStyle(QFrame::NoFrame);
  m_Prompt->setPlaceholderText(
      tr("Ask Bread AI... (Enter sends; Shift+Enter inserts a new line)"));
  m_Prompt->setToolTip(
      tr("Type @ to load a Skill or MCP server for this message (@skill:<id>, @mcp:<id>)."));
  promptEdit->mentionRequested = [this, promptEdit]() {
    if(m_MentionSkillIds.isEmpty() && m_MentionMcpIds.isEmpty())
    {
      refreshToolMentionCatalog();
      return;
    }
    QMenu menu(promptEdit);
    // MSVC counts a direct QObject::connect template call inside a lambda as an implicit
    // use of `this`; route it through the file's free helper so this lambda stays
    // capture-clean (same reason connectAIConfigurationSignal exists).
    const auto addEntries = [&menu, promptEdit](const QString &prefix, const QStringList &ids) {
      for(const QString &id : ids)
      {
        // i18n-ignore: composer mention syntax inserted verbatim.
        const QString token = prefix + id;
        QAction *action = menu.addAction(token);
        connectAIConfigurationSignal(action, &QAction::triggered, promptEdit,
                                     [promptEdit, token]() {
                                       // The "@" that opened this menu is already in the buffer.
                                       promptEdit->insertPlainText(token.mid(1) + lit(" "));
                                     });
      }
    };
    if(!m_MentionSkillIds.isEmpty())
    {
      menu.addSection(tr("Skills"));
      addEntries(lit("@skill:"), m_MentionSkillIds);    // i18n-ignore: composer mention syntax.
    }
    if(!m_MentionMcpIds.isEmpty())
    {
      menu.addSection(tr("MCP servers"));
      addEntries(lit("@mcp:"), m_MentionMcpIds);    // i18n-ignore: composer mention syntax.
    }
    const QRect caret = promptEdit->cursorRect();
    menu.exec(promptEdit->mapToGlobal(caret.bottomLeft()));
  };
  composerLayout->addWidget(m_Prompt);

  QHBoxLayout *sendRow = new QHBoxLayout();
  m_ProfileCombo = new QComboBox(this);
  m_ProfileCombo->setToolTip(tr("AI connection"));
  m_ProfileCombo->setMinimumContentsLength(12);
  m_ProfileCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
  m_ModelCombo = new QComboBox(this);
  m_ModelCombo->setToolTip(tr("Model used for this request"));
  m_ModelCombo->setMinimumContentsLength(12);
  m_ModelCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
  m_QueueStatus = new QLabel(this);
  m_QueueStatus->setTextInteractionFlags(Qt::TextSelectableByMouse);
  m_AddAttachment = new QPushButton(composer);
  m_AddAttachment->setIcon(
      assistantActionIcon(m_AddAttachment, AssistantActionIcon::Add));
  m_AddAttachment->setToolTip(tr("Add images or files"));
  m_AddAttachment->setAccessibleName(tr("Add attachment"));
  m_AddAttachment->setFixedSize(32, 32);
  m_AddAttachment->setFlat(true);
  m_AnnotateRT = new QPushButton(composer);
  // i18n-ignore: stable QObject identifier used by native UI tests.
  m_AnnotateRT->setObjectName(QLatin1String("aiAnnotateRenderTarget"));
  m_AnnotateRT->setIcon(assistantActionIcon(m_AnnotateRT, AssistantActionIcon::Brush));
  m_AnnotateRT->setToolTip(tr("Draw on the selected render target to ask Bread AI about a region"));
  m_AnnotateRT->setAccessibleName(tr("Annotate render target"));
  m_AnnotateRT->setFixedSize(32, 32);
  m_AnnotateRT->setFlat(true);
  m_Cancel = new QPushButton(composer);
  m_Cancel->setIcon(assistantActionIcon(m_Cancel, AssistantActionIcon::Stop));
  m_Cancel->setToolTip(tr("Stop the active chat request."));
  m_Cancel->setAccessibleName(tr("Stop"));
  m_Cancel->setFixedSize(36, 36);
  m_Cancel->setFlat(true);
  m_Cancel->setStyleSheet(
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QPushButton { border: 1px solid palette(mid); border-radius: 18px; "
          "background: transparent; } QPushButton:hover { background: palette(midlight); }"));
  m_Cancel->setVisible(false);
  m_Send = new QPushButton(composer);
  m_Send->setToolTip(tr("Send"));
  m_Send->setAccessibleName(tr("Send"));
  m_Send->setFixedSize(36, 36);
  m_Send->setFlat(true);
  QPalette sendPalette = m_Send->palette();
  sendPalette.setColor(QPalette::ButtonText, sendPalette.color(QPalette::HighlightedText));
  m_Send->setPalette(sendPalette);
  m_Send->setIcon(assistantActionIcon(m_Send, AssistantActionIcon::Send));
  m_Send->setStyleSheet(
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QPushButton { border: none; border-radius: 18px; "
          "background: palette(highlight); color: palette(highlighted-text); } "
          "QPushButton:hover { background: palette(link); } "
          "QPushButton:disabled { background: palette(mid); }"));
  sendRow->setContentsMargins(0, 0, 0, 0);
  sendRow->setSpacing(6);
  sendRow->addWidget(m_AddAttachment, 0, Qt::AlignBottom);
  sendRow->addWidget(m_AnnotateRT, 0, Qt::AlignBottom);
  sendRow->addWidget(m_QueueStatus, 0, Qt::AlignBottom);
  sendRow->addStretch(1);
  sendRow->addWidget(m_ProfileCombo, 0, Qt::AlignBottom);
  sendRow->addWidget(m_ModelCombo, 0, Qt::AlignBottom);
  sendRow->addWidget(m_Cancel, 0, Qt::AlignBottom);
  sendRow->addWidget(m_Send, 0, Qt::AlignBottom);
  composerLayout->addLayout(sendRow);
  mainLayout->addWidget(composer);

  connect(connections, &QToolButton::clicked, this, [this]() { manageConnections(); });
  connect(agentMonitor, &QToolButton::clicked, this, [this]() {
    if(!m_AgentMonitor)
      return;
    const QString taskId = activeConversationTaskId();
    if(!taskId.isEmpty())
      m_AgentMonitor->OpenTask(taskId);
    else
      m_AgentMonitor->OpenEmpty();
  });
  connect(m_ProfileCombo, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged),
          this, [this](int index) { selectProfile(index); });
  connect(m_ModelCombo, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged),
          this, [this](int index) { selectModel(index); });
  connect(explain, &QPushButton::clicked, this, [this]() {
    if(!m_Ctx.IsCaptureLoaded())
    {
      appendSystem(tr("This action requires an open capture."), true);
      return;
    }
    StartCaptureIntent(
        tr("Use @renderdoc-explain-event to reorganize this entire frame's rendering flow. "
           "Follow the skill workflow, inspect the capture evidence again, and update the "
           "Frame Rendering Flow window, including any existing diagram."));
  });
  connect(shader, &QPushButton::clicked, this,
          [this, shader]() { showShaderIntentMenu(shader); });
  connect(newConversation, &QPushButton::clicked, this, [this]() { createConversation(); });
  connect(m_ClarificationSubmit, &QPushButton::clicked, this,
          [this]() { submitClarification(); });
  m_ConversationList->setSelectHandler(
      [this](const QString &conversationId) { selectConversation(conversationId); });
  m_ConversationList->setPinHandler(
      [this](const QString &conversationId, bool) { toggleConversationPinned(conversationId); });
  m_ConversationList->setDeleteHandler(
      [this](const QString &conversationId) { deleteConversation(conversationId); });
  connect(m_AddAttachment, &QPushButton::clicked, this,
          [this]() { chooseAttachmentFiles(); });
  connect(m_AnnotateRT, &QPushButton::clicked, this, [this]() { annotateRenderTarget(); });
  connect(m_Send, &QPushButton::clicked, this, [this]() {
    const QString prompt = m_Prompt->toPlainText().trimmed();
    // Attachments are reference data, never an implicit request. Require the user to state what
    // should be done with them instead of deriving authority from an attached document.
    if(!prompt.isEmpty())
    {
      for(const AIChatAttachment &attachment : m_ComposerAttachments)
      {
        if(isPathReferenceAttachment(attachment) &&
           !pathReferenceMatchesCurrentFile(attachment))
        {
          appendSystem(
              tr("A referenced file changed after it was added. Remove it and add it again."),
              true);
          return;
        }
      }
      const int queueSize = m_ChatQueue.size();
      const QVector<AIChatAttachment> attachments = m_ComposerAttachments;
      startChat(prompt, true, QString(), ClarificationResponse(), QString(),
                attachments);
      if(m_ChatQueue.size() > queueSize)
      {
        m_Prompt->clear();
        m_ComposerAttachments.clear();
        m_ComposerSystemThumbnails.clear();
        m_ComposerSystemAnimations.clear();
        rebuildAttachmentChips();
      }
    }
    else if(!m_ComposerAttachments.isEmpty())
    {
      appendSystem(tr("Add a message describing what the AI should do with the attachments."),
                   true);
    }
  });
  promptEdit->send = [this]() { m_Send->click(); };
  promptEdit->insertAttachments =
      [this](const QMimeData *mimeData) { return addMimeAttachments(mimeData); };
  composer->insertAttachments =
      [this](const QMimeData *mimeData) { return addMimeAttachments(mimeData); };
  connect(m_Cancel, &QPushButton::clicked, this,
          [this]() { cancelChat(lit("cancelled"), true); });

  m_EventTimer = new QTimer(this);
  m_EventTimer->setInterval(TaskEventPollIntervalMs);
  connect(m_EventTimer, &QTimer::timeout, this, [this]() { pollTaskEvents(); });

  m_ConnectionStatusTimer = new QTimer(this);
  m_ConnectionStatusTimer->setSingleShot(true);
  m_ConnectionStatusTimer->setInterval(1000);
  connect(m_ConnectionStatusTimer, &QTimer::timeout, this, [this]() { refreshConnectionStatus(); });

  m_ConversationSaveTimer = new QTimer(this);
  m_ConversationSaveTimer->setSingleShot(true);
  connect(m_ConversationSaveTimer, &QTimer::timeout, this, [this]() { saveConversations(); });

  m_ConversationRenderTimer = new QTimer(this);
  m_ConversationRenderTimer->setSingleShot(true);
  m_ConversationRenderTimer->setInterval(60);
  connect(m_ConversationRenderTimer, &QTimer::timeout, this,
          [this]() { renderConversation(); });

  m_ChatContextFreezeTimer = new QTimer(this);
  m_ChatContextFreezeTimer->setInterval(ChatContextFreezePollIntervalMs);
  connect(m_ChatContextFreezeTimer, &QTimer::timeout, this,
          [this]() { pollChatContextFreeze(); });

  // This native timer continues independently of AgentHost heartbeats and Python. A stall is
  // measured only after control returns to Qt, without polling Python or doing file I/O here.
  struct GuiHeartbeatState
  {
    AIGuiHeartbeat gaps;
    QElapsedTimer clock;
    QString phase;
    QString taskId;
  };
  const auto heartbeatState = std::make_shared<GuiHeartbeatState>();
  heartbeatState->clock.start();
  QTimer *guiHeartbeat = new QTimer(this);
  guiHeartbeat->setInterval(250);
  connect(guiHeartbeat, &QTimer::timeout, this, [this, heartbeatState]() {
    if(m_ShuttingDown)
      return;
    QString phase;
    QString taskId;
    // i18n-ignore: stable diagnostic phase identifiers, not user-facing labels.
    if(!m_ChatContextFreezeJobId.isEmpty())
    {
      // i18n-ignore: stable diagnostic phase identifier.
      phase = lit("context-freeze");
      taskId = m_ChatContextFreezeTaskId;
    }
    else if(!m_ChatQueue.isEmpty())
    {
      // i18n-ignore: stable diagnostic phase identifiers.
      phase = m_ChatQueue.first().contextFreezeRequired ? lit("context-start") : lit("queued");
      taskId = m_ChatQueue.first().taskId;
    }
    else if(!m_ChatExecutions.isEmpty())
    {
      // i18n-ignore: stable diagnostic phase identifier.
      phase = lit("agent-running");
      taskId = m_ChatExecutions.constBegin().key();
    }
    const qint64 gap = heartbeatState->gaps.sample(heartbeatState->clock.elapsed(), !phase.isEmpty());
    if(gap > 0)
    {
      QJsonObject record;
      // i18n-ignore: machine-readable native GUI timing diagnostic schema and fields.
      record[lit("schema")] = lit("bread-ai-gui-responsiveness/v1");
      // i18n-ignore: diagnostic timestamp field.
      record[lit("timestampUnixMs")] = QString::number(QDateTime::currentMSecsSinceEpoch());
      // i18n-ignore: diagnostic process ID field.
      record[lit("pid")] = QString::number(QCoreApplication::applicationPid());
      // i18n-ignore: diagnostic event-loop timing field.
      record[lit("gapMs")] = double(gap);
      // i18n-ignore: diagnostic previous phase field.
      record[lit("phaseBefore")] = heartbeatState->phase;
      // i18n-ignore: diagnostic current phase field.
      record[lit("phaseAfter")] = phase;
      // i18n-ignore: diagnostic previous task ID field.
      record[lit("taskIdBefore")] = heartbeatState->taskId;
      // i18n-ignore: diagnostic current task ID field.
      record[lit("taskIdAfter")] = taskId;
      const QByteArray encoded = QJsonDocument(record).toJson(QJsonDocument::Compact);
      // i18n-ignore: structured developer diagnostic also forwarded to RenderDoc's log handler.
      qWarning().noquote() << QString::fromUtf8(encoded);
      persistAIGuiGap(encoded + '\n');
    }
    heartbeatState->phase = phase;
    heartbeatState->taskId = taskId;
  });
  guiHeartbeat->start();

  const bool captureWasLoaded = m_Ctx.IsCaptureLoaded();
  m_CLIPath = findAgentCLI();
  // A confirmed deletion may have outlived the process which was cleaning its files.
  // Resume only those persisted requests, independently of the active capture/chat.
  if(!m_CLIPath.isEmpty() &&
     !QDir(QDir(taskEventRoot()).filePath(QStringLiteral(".cleanup-pending")))
          .entryList(QStringList() << QStringLiteral("*.json"), QDir::Files | QDir::NoSymLinks).isEmpty())
    runCLIAsync({QStringLiteral("conversation-cleanup"), QStringLiteral("--pending"),
                  QStringLiteral("--json")}, QByteArray(), 30000, this,
                [this](int exitCode, const QByteArray &output, const QString &diagnostic, bool) {
                  const QJsonObject receipt = QJsonDocument::fromJson(output).object();
                  // i18n-ignore: Background cleanup receipt protocol fields.
                  for(const QJsonValue &task : receipt.value(QStringLiteral("taskIds")).toArray())
                    if(safeTaskId(task.toString()))
                      forgetDeletedTask(task.toString());
                  if(exitCode != 0 || !receipt.value(QStringLiteral("complete")).toBool())
                    appendSystem(tr("The conversation was deleted, but some task files could not "
                                    "be cleaned up. Saved work was kept. %1").arg(diagnostic), true);
                });
  refreshProfiles();
  refreshToolMentionCatalog();
  updateCaptureStatus();
  m_Ctx.AddCaptureViewer(this);
  if(!captureWasLoaded)
    loadConversations();
  updateComposerState();

  if(m_CLIPath.isEmpty())
    appendSystem(tr("AI host not found. Set REPLAYLAB_AI_CLI or install the bundled AI host."),
                 true);
  else
    appendSystem(tr("Bread AI is embedded here. Provider processes remain isolated from replay."));
}

AIAssistant::~AIAssistant()
{
  Shutdown();
  delete m_AgentMonitor;
  m_AgentMonitor = NULL;
  delete m_AgentTraceStore;
  m_AgentTraceStore = NULL;
  forceStopProcessesForDestruction();
  m_Ctx.RemoveCaptureViewer(this);
  m_Ctx.BuiltinWindowClosed(this);
}

void AIAssistant::Shutdown()
{
  if(m_ShuttingDown)
    return;
  m_ShuttingDown = true;
  discardAllChatContextFreezes();
  if(m_EventTimer)
    m_EventTimer->stop();
  if(m_ConnectionStatusTimer)
    m_ConnectionStatusTimer->stop();
  if(m_ConversationRenderTimer)
    m_ConversationRenderTimer->stop();
  if(m_ChatContextFreezeTimer)
    m_ChatContextFreezeTimer->stop();
  cancelConfigurationProcesses();
  if(m_AgentMonitor)
    m_AgentMonitor->HideAll();
  // Ask AgentHost to cancel its asyncio task first so it can append a durable
  // `cancelled` event and clean up provider/tool children. The bounded fallback
  // below still guarantees that no callback races extension destruction. Signal
  // every independent conversation before waiting, then share one total grace
  // period instead of blocking the GUI thread for 2.5 seconds per process.
  const QStringList runningTaskIds = m_ChatExecutions.keys();
  for(const QString &taskId : runningTaskIds)
    requestChatCancellation(chatExecution(taskId));
  QElapsedTimer cancellationDeadline;
  cancellationDeadline.start();
  for(const QString &taskId : runningTaskIds)
    // i18n-ignore: Stable internal task-state protocol value; never shown as UI text.
    cancelChat(lit("interrupted"), false, taskId,
               qMax(0, 2500 - int(cancellationDeadline.elapsed())), false);
  forceStopProcessesForDestruction();
  saveConversations();
  if(m_AgentTraceStore)
  {
    const QString eventRoot = taskEventRoot();
    // Closing the AI panel archives finished task logs so the monitor can mark
    // them as old; files older than 7 days are then removed. Active runs were
    // cancelled above and keep their logs until archived by conversation delete
    // or a later close after they finish.
    for(auto it = m_CompletedTasks.cbegin(); it != m_CompletedTasks.cend(); ++it)
    {
      if(safeTaskId(*it))
        // i18n-ignore: stable internal trace-archive reason ID, never shown as UI text.
        m_AgentTraceStore->ArchiveTaskLog(*it, eventRoot, lit("assistant-closed"));
    }
    m_AgentTraceStore->PurgeExpiredArchivedTaskLogs(eventRoot, 7);
  }
}

bool AIAssistant::AddTextAttachment(const QString &title, const QString &text,
                                    const QString &source, bool code,
                                    const QString &contextJSON)
{
  AIChatAttachment attachment;
  attachment.id = lit("attachment-") + newTaskId();    // i18n-ignore: stable local identifier
  attachment.kind = code ? lit("code") : lit("text");    // i18n-ignore: attachment kind IDs
  attachment.title = title.simplified().left(512);
  if(attachment.title.isEmpty())
    attachment.title = tr("Selected text");
  attachment.mediaType =
      code ? lit("text/x-hlsl") : lit("text/plain");    // i18n-ignore: MIME type IDs
  attachment.source = source.simplified().left(32768);
  attachment.encoding = lit("utf-8");    // i18n-ignore: wire encoding identifier
  attachment.content = text;
  attachment.originalBytes = attachment.content.toUtf8().size();
  if(!contextJSON.trimmed().isEmpty())
  {
    const QByteArray encodedContext = contextJSON.toUtf8();
    QJsonParseError parseError;
    const QJsonDocument contextDocument = QJsonDocument::fromJson(encodedContext, &parseError);
    if(encodedContext.size() > MaxAttachmentContextBytes ||
       parseError.error != QJsonParseError::NoError || !contextDocument.isObject() ||
       !validAttachmentContext(contextDocument.object()))
    {
      appendSystem(tr("The selected text context is invalid and was not attached."), true);
      return false;
    }
    attachment.context = contextDocument.object();
    if(attachment.context.value(QLatin1String("origin")).toObject()
           .value(QLatin1String("kind")).toString() == QLatin1String("resource-selection"))
    {
      attachment.kind = QLatin1String("text");
      attachment.mediaType = QLatin1String("application/json");
    }
    QJsonObject capture = attachment.context.value(QLatin1String("capture")).toObject();
    if(m_Ctx.IsCaptureLoaded() && !capture.contains(QLatin1String("captureSessionId")))
    {
      QString sessionId;
      if(findBridgeBinding(NULL, &sessionId))
      {
        capture[QLatin1String("captureSessionId")] = sessionId;
        attachment.context[QLatin1String("capture")] = capture;
      }
    }
  }

  QString error;
  if(!addComposerAttachment(attachment, &error))
  {
    if(!error.isEmpty())
      appendSystem(error, true);
    return false;
  }
  return true;
}

bool AIAssistant::addComposerAttachment(const AIChatAttachment &attachment, QString *error)
{
  if(!safeTaskId(attachment.id) || !validAttachmentKind(attachment.kind) ||
     attachment.title.isEmpty() || attachment.title.size() > 512 ||
     attachment.title.contains(QChar::Null) || attachment.title.contains(QLatin1Char('\n')) ||
     attachment.title.contains(QLatin1Char('\r')) || attachment.mediaType.size() > 160 ||
     !validAttachmentMediaType(attachment.mediaType) || attachment.source.size() > 32768 ||
     attachment.source.contains(QChar::Null) || attachment.source.contains(QLatin1Char('\n')) ||
     attachment.source.contains(QLatin1Char('\r')) || attachment.content.contains(QChar::Null) ||
     !validAttachmentContext(attachment.context) ||
     attachment.originalBytes <= 0 ||
     attachment.truncated)
  {
    if(error)
      *error = tr("The attachment metadata is invalid.");
    return false;
  }

  qint64 contentBytes = 0;
  if(!attachmentDecodedBytes(attachment, &contentBytes) ||
     attachment.originalBytes != contentBytes)
  {
    if(error)
      *error = tr("The attachment content or byte count is invalid.");
    return false;
  }

  if(isPathReferenceAttachment(attachment))
  {
    if(!pathReferenceMatchesCurrentFile(attachment))
    {
      if(error)
        *error = tr("The file reference changed or is not a safe regular file. Add it again.");
      return false;
    }
  }
  else if(isTextAttachment(attachment))
  {
    if(attachment.encoding != QLatin1String("utf-8") ||
       !validTextAttachmentMediaType(attachment.mediaType))
    {
      if(error)
        *error = tr("The text attachment metadata is invalid.");
      return false;
    }
    if(attachment.content.trimmed().isEmpty())
    {
      if(error)
        *error = tr("The attachment is empty.");
      return false;
    }
    if(attachment.content.size() > MaxAttachmentContentChars ||
       contentBytes > MaxAttachmentUTF8Bytes)
    {
      if(error)
        *error = tr("The text attachment is too large. The limit is 1 MiB per item.");
      return false;
    }
  }
  else
  {
    if(attachment.encoding != QLatin1String("base64") ||
       contentBytes > MaxBinaryAttachmentBytes)
    {
      if(error)
        *error = tr("The file attachment is too large. The limit is 8 MiB per item.");
      return false;
    }
    if(attachment.kind == QLatin1String("image"))
    {
      QByteArray bytes;
      if(!validRasterImageMediaType(attachment.mediaType) ||
         !decodeCanonicalBase64(attachment.content, &bytes) ||
         !binaryImageHeaderIsSafe(bytes, attachment.mediaType))
      {
        if(error)
          *error = tr("The image attachment is invalid or is not a supported raster image.");
        return false;
      }
    }
  }
  if(m_ComposerAttachments.size() >= MaxChatAttachments)
  {
    if(error)
      *error = tr("A request can contain at most %1 attachments.").arg(MaxChatAttachments);
    return false;
  }

  qint64 totalTextBytes = isTextAttachment(attachment) ? contentBytes : 0;
  qint64 totalBinaryBytes = isBinaryAttachment(attachment) ? contentBytes : 0;
  for(const AIChatAttachment &existing : m_ComposerAttachments)
  {
    if(existing.kind == attachment.kind && existing.source == attachment.source &&
       existing.content == attachment.content && existing.context == attachment.context)
    {
      if(error)
        *error = tr("This attachment is already in the composer.");
      return false;
    }
    qint64 existingBytes = 0;
    if(!attachmentDecodedBytes(existing, &existingBytes))
    {
      if(error)
        *error = tr("An existing attachment is invalid. Remove it before continuing.");
      return false;
    }
    if(isTextAttachment(existing))
      totalTextBytes += existingBytes;
    else if(isBinaryAttachment(existing))
      totalBinaryBytes += existingBytes;
  }
  if(totalTextBytes > MaxAttachmentTotalUTF8Bytes)
  {
    if(error)
      *error = tr("The attachments exceed the 2 MiB text limit for one request.");
    return false;
  }
  if(totalBinaryBytes > MaxBinaryAttachmentTotalBytes)
  {
    if(error)
      *error = tr("The attachments exceed the 8 MiB file limit for one request.");
    return false;
  }

  m_ComposerAttachments.push_back(attachment);
  rebuildAttachmentChips();
  if(m_Prompt)
    m_Prompt->setFocus(Qt::OtherFocusReason);
  return true;
}

void AIAssistant::chooseAttachmentFiles()
{
  const QStringList paths = QFileDialog::getOpenFileNames(
      this, tr("Add attachments"), QString(), tr("All files (*)"));
  if(!paths.isEmpty())
    addAttachmentFiles(paths);
}

void AIAssistant::annotateRenderTarget()
{
  if(!m_Ctx.IsCaptureLoaded())
  {
    appendSystem(tr("This action requires an open capture."), true);
    return;
  }
  QString session;
  if(!findBridgeBinding(NULL, &session) || session.isEmpty())
  {
    appendSystem(tr("The capture connection is not ready. Try drawing again after it connects."),
                 true);
    return;
  }
  m_Ctx.ShowTextureViewer();
  TextureViewer *viewer = dynamic_cast<TextureViewer *>(m_Ctx.GetTextureViewer()->Widget());
  if(!viewer)
    return;
  QJsonObject capture;
  capture[QLatin1String("captureSessionId")] = session;
  // Resume the newest draft for this exact capture/view. Sent messages are immutable;
  // editing those is an explicit transcript action that creates a fresh draft.
  for(int i = m_ComposerAttachments.size() - 1; i >= 0; --i)
  {
    const AIChatAttachment &item = m_ComposerAttachments[i];
    if(item.annotationImageRole() == QLatin1String("annotated") &&
       item.context.value(QLatin1String("capture"))
               .toObject()
               .value(QLatin1String("captureSessionId"))
               .toString() == session &&
       viewer->MatchesAIAnnotation(item.context))
    {
      editRenderTargetAnnotation(item.id);
      return;
    }
  }
  QPointer<AIAssistant> self(this);
  const QString conversationId = m_ActiveConversationId;
  viewer->BeginAIAnnotation(
      capture,
      [self, conversationId](const QImage &original, const QImage &marked, const QJsonObject &context) {
        if(!self)
          return false;
        if(self->m_ActiveConversationId != conversationId)
        {
          self->appendSystem(
              tr("Return to the conversation where you started drawing before adding the marks."),
              true);
          return false;
        }
        return self->addRenderTargetAnnotation(original, marked, context);
      },
      [self](const QString &error) {
        if(self)
          self->appendSystem(error, true);
      });
}

bool AIAssistant::addRenderTargetAnnotation(const QImage &original, const QImage &annotated,
                                            const QJsonObject &context)
{
  const QVector<AIChatAttachment> before = m_ComposerAttachments;
  const QString pair =
      context.value(QLatin1String("annotation")).toObject().value(QLatin1String("pairId")).toString();
  QVector<AIChatAttachment> remaining;
  for(const AIChatAttachment &item : before)
    if(item.annotationPairId() != pair || pair.isEmpty())
      remaining.push_back(item);
  if(remaining.size() + 2 > MaxChatAttachments)
  {
    appendSystem(tr("An annotation needs two attachment slots for the original and marked images. "
                    "Remove some attachments first."),
                 true);
    return false;
  }
  QByteArray encoded[2];
  QImage images[] = {original, annotated};
  // The editor uses native RT pixels. The attachment protocol caps each encoded image at 1600.
  const QSize limit(1600, 1600);
  for(QImage &image : images)
  {
    if(image.width() <= limit.width() && image.height() <= limit.height())
      continue;
    const QSize scaled = image.size().scaled(limit, Qt::KeepAspectRatio).expandedTo(QSize(1, 1));
    image = image.scaled(scaled, Qt::IgnoreAspectRatio, Qt::SmoothTransformation);
  }
  bool jpeg = false;
  for(int attempt = 0; attempt < 2; ++attempt)
  {
    jpeg = attempt > 0;
    for(int i = 0; i < 2; ++i)
    {
      encoded[i].clear();
      QBuffer buffer(&encoded[i]);
      buffer.open(QIODevice::WriteOnly);
      if(!images[i].save(&buffer, jpeg ? "JPEG" : "PNG", jpeg ? 93 : -1))
        return false;
    }
    if(encoded[0].size() + encoded[1].size() <= MaxBinaryAttachmentTotalBytes)
      break;
  }
  const uint32_t eid = uint32_t(
      context.value(QLatin1String("capture")).toObject().value(QLatin1String("eventId")).toDouble());
  QString error;
  m_ComposerAttachments = remaining;
  for(int i = 0; i < 2; ++i)
  {
    AIChatAttachment attachment;
    // i18n-ignore: stable attachment transport identifiers and MIME types.
    attachment.id = QLatin1String("annotation-") + pair + QString::number(i);
    attachment.kind = QLatin1String("image");
    attachment.encoding = QLatin1String("base64");
    attachment.mediaType = jpeg ? QLatin1String("image/jpeg") : QLatin1String("image/png");
    attachment.source = QLatin1String("renderdoc-rt-annotation");
    attachment.title = i == 0 ? tr("EID %1: original render target").arg(eid)
                              : tr("EID %1: marked render target").arg(eid);
    attachment.content = QString::fromLatin1(encoded[i].toBase64());
    attachment.originalBytes = encoded[i].size();
    attachment.context = context;
    QJsonObject annotation = context.value(QLatin1String("annotation")).toObject();
    annotation[QLatin1String("imageWidth")] = images[i].width();
    annotation[QLatin1String("imageHeight")] = images[i].height();
    annotation[QLatin1String("imageRole")] =
        i == 0 ? QLatin1String("original") : QLatin1String("annotated");
    attachment.context[QLatin1String("annotation")] = annotation;
    if(!addComposerAttachment(attachment, &error))
    {
      // Keep the pair atomic, including when other attachments have consumed the byte budget.
      m_ComposerAttachments = before;
      rebuildAttachmentChips();
      appendSystem(error, true);
      return false;
    }
  }
  return true;
}

void AIAssistant::editRenderTargetAnnotation(const QString &attachmentId)
{
  QVector<AIChatAttachment> items = m_ComposerAttachments;
  bool fromHistory = false;
  auto hasId = [&attachmentId](const QVector<AIChatAttachment> &rows) {
    for(const AIChatAttachment &item : rows)
      if(item.id == attachmentId)
        return true;
    return false;
  };
  if(!hasId(items))
  {
    const ConversationThread *thread = conversation();
    if(thread)
      for(const AIChatMessage &message : thread->messages)
        if(hasId(message.attachments))
        {
          items = message.attachments;
          fromHistory = true;
          break;
        }
  }
  AIChatAttachment selected, original;
  for(const AIChatAttachment &item : items)
    if(item.id == attachmentId)
      selected = item;
  if(selected.annotationPairId().isEmpty())
    return;
  for(const AIChatAttachment &item : items)
    if(item.annotationPairId() == selected.annotationPairId() &&
       item.annotationImageRole() == QLatin1String("original"))
      original = item;
  const QImage image = attachmentPreviewPixmap(original).toImage();
  if(image.isNull())
  {
    appendSystem(tr("The original annotation image is unavailable. The saved marks were kept."),
                 true);
    return;
  }
  QJsonObject context = selected.context;
  if(fromHistory)
  {
    QJsonObject annotation = context.value(QLatin1String("annotation")).toObject();
    annotation[QLatin1String("pairId")] = newTaskId();
    context[QLatin1String("annotation")] = annotation;
  }
  m_Ctx.ShowTextureViewer();
  TextureViewer *viewer = dynamic_cast<TextureViewer *>(m_Ctx.GetTextureViewer()->Widget());
  if(!viewer)
    return;
  QPointer<AIAssistant> self(this);
  const QString conversationId = m_ActiveConversationId;
  viewer->EditAIAnnotation(
      image, selected.title, context,
      [self, conversationId](const QImage &original, const QImage &marked, const QJsonObject &context) {
        if(!self)
          return false;
        if(self->m_ActiveConversationId != conversationId)
        {
          self->appendSystem(
              tr("Return to the conversation where you started drawing before adding the marks."),
              true);
          return false;
        }
        return self->addRenderTargetAnnotation(original, marked, context);
      });
}

void AIAssistant::requestSystemThumbnail(const QString &attachmentId,
                                         const QString &canonicalPath, qint64 expectedBytes,
                                         qint64 expectedModifiedUnixMs,
                                         bool preferRasterDecoder)
{
  QPointer<AIAssistant> assistant(this);
  aiBackgroundThreadPool()->start(new FunctionRunnable(
      [assistant, attachmentId, canonicalPath, expectedBytes, expectedModifiedUnixMs,
       preferRasterDecoder]() {
        QString currentCanonicalPath;
        const QFileInfo before(canonicalPath);
        if(!fileInfoIsSafeReferenceTarget(before, &currentCanonicalPath) ||
           currentCanonicalPath != canonicalPath || before.size() != expectedBytes ||
           before.lastModified().toMSecsSinceEpoch() != expectedModifiedUnixMs)
          return;

        QImage thumbnail;
        AIFileThumbnail::RasterAnimationResult animation;
        if(preferRasterDecoder && expectedBytes <= MaxPathReferenceAnimatedImageBytes)
        {
          animation = AIFileThumbnail::decodeGIFAnimationFile(
              canonicalPath, QSize(96, 96), MaxPathReferenceAnimatedImageBytes,
              MaxAnimatedImageFrames, MaxAnimatedImagePixelFrames);
          if(!animation.isNull())
            thumbnail = animation.frames[0];
        }
        if(preferRasterDecoder && expectedBytes <= MaxPathReferenceImagePreviewBytes)
        {
          if(thumbnail.isNull())
            thumbnail = AIFileThumbnail::decodeRasterFile(
                            canonicalPath, QSize(96, 96),
                            MaxPathReferenceImagePreviewBytes)
                            .image;
        }
        if(thumbnail.isNull())
          thumbnail = AIFileThumbnail::loadSystemThumbnail(canonicalPath, QSize(96, 96));

        QString finalCanonicalPath;
        const QFileInfo after(canonicalPath);
        if(thumbnail.isNull() || !assistant ||
           !fileInfoIsSafeReferenceTarget(after, &finalCanonicalPath) ||
           finalCanonicalPath != canonicalPath || after.size() != expectedBytes ||
           after.lastModified().toMSecsSinceEpoch() != expectedModifiedUnixMs)
          return;

        QTimer::singleShot(
            0, assistant.data(),
            [assistant, attachmentId, canonicalPath, expectedBytes, expectedModifiedUnixMs,
             thumbnail, animation]() {
              if(!assistant)
                return;
              QString callbackCanonicalPath;
              const QFileInfo callbackFile(canonicalPath);
              if(!fileInfoIsSafeReferenceTarget(callbackFile, &callbackCanonicalPath) ||
                 callbackCanonicalPath != canonicalPath || callbackFile.size() != expectedBytes ||
                 callbackFile.lastModified().toMSecsSinceEpoch() != expectedModifiedUnixMs)
                return;
              for(const AIChatAttachment &attachment : assistant->m_ComposerAttachments)
              {
                if(attachment.id != attachmentId)
                  continue;
                if(isPathReferenceAttachment(attachment) &&
                   !pathReferenceMatchesCurrentFile(attachment))
                  return;
                assistant->m_ComposerSystemThumbnails.insert(attachmentId, thumbnail);
                if(!animation.isNull())
                {
                  ComposerRasterAnimation cachedAnimation;
                  cachedAnimation.frames = animation.frames;
                  cachedAnimation.frameDelaysMs = animation.frameDelaysMs;
                  cachedAnimation.sourceSize = animation.sourceSize;
                  assistant->m_ComposerSystemAnimations.insert(attachmentId, cachedAnimation);
                }
                assistant->rebuildAttachmentChips();
                return;
              }
            });
      }), -1);
}

void AIAssistant::requestInlineRasterThumbnail(const QString &attachmentId,
                                               const QByteArray &encodedBytes)
{
  if(encodedBytes.isEmpty() || encodedBytes.size() > MaxBinaryAttachmentBytes)
    return;
  QPointer<AIAssistant> assistant(this);
  aiBackgroundThreadPool()->start(new FunctionRunnable(
      [assistant, attachmentId, encodedBytes]() {
        AIFileThumbnail::RasterAnimationResult animation =
            AIFileThumbnail::decodeGIFAnimationData(
                encodedBytes, QSize(96, 96), MaxBinaryAttachmentBytes,
                MaxAnimatedImageFrames, MaxAnimatedImagePixelFrames);
        QImage thumbnail;
        if(!animation.isNull())
          thumbnail = animation.frames[0];
        if(thumbnail.isNull())
          thumbnail = AIFileThumbnail::decodeRasterData(
                          encodedBytes, QSize(96, 96), MaxBinaryAttachmentBytes)
                          .image;
        if(thumbnail.isNull() || !assistant)
          return;

        QTimer::singleShot(
            0, assistant.data(),
            [assistant, attachmentId, expectedBytes = encodedBytes.size(), thumbnail,
             animation]() {
              if(!assistant)
                return;
              for(const AIChatAttachment &attachment : assistant->m_ComposerAttachments)
              {
                if(attachment.id != attachmentId || attachment.kind != QLatin1String("file") ||
                   attachment.encoding != QLatin1String("base64") ||
                   attachment.originalBytes != expectedBytes)
                  continue;
                assistant->m_ComposerSystemThumbnails.insert(attachmentId, thumbnail);
                if(!animation.isNull())
                {
                  ComposerRasterAnimation cachedAnimation;
                  cachedAnimation.frames = animation.frames;
                  cachedAnimation.frameDelaysMs = animation.frameDelaysMs;
                  cachedAnimation.sourceSize = animation.sourceSize;
                  assistant->m_ComposerSystemAnimations.insert(attachmentId, cachedAnimation);
                }
                assistant->rebuildAttachmentChips();
                return;
              }
            });
      }), -1);
}

bool AIAssistant::addAttachmentFiles(const QStringList &paths)
{
  bool addedAny = false;
  for(const QString &path : paths)
  {
    if(m_ComposerAttachments.size() >= MaxChatAttachments)
    {
      appendSystem(tr("A request can contain at most %1 attachments.").arg(MaxChatAttachments),
                   true);
      break;
    }
    QFileInfo info(path);
    if(!info.exists() || !info.isFile())
    {
      appendSystem(tr("The attachment is not a readable file: %1").arg(path), true);
      continue;
    }
    if(info.size() <= 0)
    {
      appendSystem(tr("The attachment is empty: %1").arg(info.fileName()), true);
      continue;
    }
    const qint64 selectedSize = info.size();
    const qint64 selectedModifiedMs = info.lastModified().toMSecsSinceEpoch();
    if(selectedSize > MaxBinaryAttachmentBytes)
    {
      QString canonicalPath;
      if(!fileInfoIsSafeReferenceTarget(info, &canonicalPath))
      {
        appendSystem(
            tr("The large attachment is not a safe regular file and cannot be referenced: %1")
                .arg(info.fileName()),
            true);
        continue;
      }
      const QFileInfo canonicalInfo(canonicalPath);
      QMimeDatabase mimeDatabase;
      QString mediaType =
          mimeDatabase.mimeTypeForFile(canonicalPath, QMimeDatabase::MatchExtension).name().left(160);
      if(!validAttachmentMediaType(mediaType))
        mediaType = lit("application/octet-stream");    // i18n-ignore: normalized MIME type

      bool hasRasterPreview = false;
      if(canonicalInfo.size() <= MaxPathReferenceImagePreviewBytes)
      {
        QImageReader reader(canonicalPath);
        QString detectedMediaType;
        hasRasterPreview =
            rasterImageReaderHeaderIsSafe(reader, NULL, &detectedMediaType);
        if(hasRasterPreview)
          mediaType = detectedMediaType;
      }

      bool hasTextPreview = false;
      if(!hasRasterPreview)
      {
        QFile previewFile(canonicalPath);
        if(previewFile.open(QIODevice::ReadOnly))
        {
          const QByteArray previewBytes = previewFile.read(MaxTextThumbnailReadBytes);
          QString previewText;
          hasTextPreview = decodeSupportedTextBytes(
              previewBytes, &previewText, MaxTextThumbnailChars,
              canonicalInfo.size() > previewBytes.size(),
              validTextAttachmentMediaType(mediaType));
          if(hasTextPreview && !validTextAttachmentMediaType(mediaType))
            mediaType = lit("text/plain");    // i18n-ignore: normalized textual MIME type
        }
      }

      AIChatAttachment attachment;
      attachment.id = lit("attachment-") + newTaskId();    // i18n-ignore: stable local identifier
      attachment.kind = lit("file-ref");    // i18n-ignore: ReplayLab attachment kind
      attachment.title = canonicalInfo.fileName().left(512);
      attachment.mediaType = mediaType;
      attachment.source = lit("file-picker");    // i18n-ignore: ReplayLab attachment origin ID
      attachment.encoding = lit("path");    // i18n-ignore: ReplayLab attachment encoding ID
      attachment.content = canonicalPath;
      attachment.originalBytes = canonicalInfo.size();
      attachment.modifiedUnixMs = canonicalInfo.lastModified().toMSecsSinceEpoch();

      const bool requestThumbnail = !hasRasterPreview && !hasTextPreview;

      QString error;
      if(addComposerAttachment(attachment, &error))
      {
        addedAny = true;
        if(requestThumbnail)
          requestSystemThumbnail(attachment.id, canonicalPath, attachment.originalBytes,
                                 attachment.modifiedUnixMs,
                                 validRasterImageMediaType(attachment.mediaType));
      }
      else
      {
        m_ComposerSystemThumbnails.remove(attachment.id);
        m_ComposerSystemAnimations.remove(attachment.id);
        if(!error.isEmpty())
          appendSystem(error, true);
      }
      continue;
    }

    QFile file(info.absoluteFilePath());
    if(!file.open(QIODevice::ReadOnly))
    {
      appendSystem(tr("The attachment could not be read: %1").arg(info.fileName()), true);
      continue;
    }
    const QByteArray bytes = file.read(MaxBinaryAttachmentBytes + 1);
    if(bytes.size() > MaxBinaryAttachmentBytes || !file.atEnd())
    {
      appendSystem(tr("The attachment is too large: %1. Files are limited to 8 MiB.")
                       .arg(info.fileName()),
                   true);
      continue;
    }

    QMimeDatabase mimeDatabase;
    const QString fileMediaType = mimeDatabase.mimeTypeForFile(info).name().left(160);
    const QString contentMediaType = mimeDatabase.mimeTypeForData(bytes).name().left(160);
    QString mediaType;
    if(validAttachmentMediaType(contentMediaType) &&
       contentMediaType != QLatin1String("application/octet-stream"))
      mediaType = contentMediaType;
    else if(validAttachmentMediaType(fileMediaType))
      mediaType = fileMediaType;
    if(!validAttachmentMediaType(mediaType))
      mediaType = lit("application/octet-stream");    // i18n-ignore: normalized binary MIME type

    AIChatAttachment attachment;
    attachment.id = lit("attachment-") + newTaskId();    // i18n-ignore: stable local identifier
    attachment.title = info.fileName().left(512);
    // The selected contents are immutable in the attachment. A provider does not need the
    // machine-local parent directory, which may contain a private user or project path.
    attachment.source = info.fileName().left(32768);
    QString detectedImageMediaType;
    const bool attachedAsImage = rasterImageBytesHeaderIsSafe(
        bytes, NULL, &detectedImageMediaType);

    bool attachedAsText = false;
    if(!attachedAsImage && bytes.size() <= MaxAttachmentUTF8Bytes)
    {
      QString contents;
      if(decodeSupportedTextBytes(bytes, &contents, MaxAttachmentContentChars + 1, false,
                                  validTextAttachmentMediaType(mediaType)) &&
         contents.size() <= MaxAttachmentContentChars)
      {
        attachment.kind = lit("text-file");    // i18n-ignore: ReplayLab attachment kind
        attachment.mediaType = validTextAttachmentMediaType(mediaType)
                                   ? mediaType
                                   : lit("text/plain");    // i18n-ignore: normalized MIME type
        attachment.encoding = lit("utf-8");    // i18n-ignore: wire encoding identifier
        attachment.content = contents;
        attachment.originalBytes = contents.toUtf8().size();
        attachedAsText = true;
      }
    }

    if(attachedAsImage)
    {
      attachment.mediaType = detectedImageMediaType;
      // i18n-ignore: ReplayLab attachment kind IDs selected by protocol media capabilities
      attachment.kind = providerNativeImageMediaType(attachment.mediaType)
                            ? lit("image")    // i18n-ignore: ReplayLab attachment kind ID
                            : lit("file");    // i18n-ignore: ReplayLab attachment kind IDs
      attachment.encoding = lit("base64");    // i18n-ignore: wire encoding identifier
      attachment.content = QString::fromLatin1(bytes.toBase64());
      attachment.originalBytes = bytes.size();
    }
    else if(!attachedAsText)
    {
      QString previewText;
      const QByteArray previewBytes = bytes.left(MaxTextThumbnailReadBytes);
      if(decodeSupportedTextBytes(previewBytes, &previewText, MaxTextThumbnailChars,
                                  previewBytes.size() < bytes.size(),
                                  validTextAttachmentMediaType(mediaType)) &&
         !validTextAttachmentMediaType(mediaType))
        mediaType = lit("text/plain");    // i18n-ignore: normalized textual MIME type
      attachment.kind = lit("file");    // i18n-ignore: ReplayLab attachment kind
      attachment.mediaType = mediaType;
      attachment.encoding = lit("base64");    // i18n-ignore: wire encoding identifier
      attachment.content = QString::fromLatin1(bytes.toBase64());
      attachment.originalBytes = bytes.size();
    }

    QString systemThumbnailPath;
    if(attachment.kind == QLatin1String("file") &&
       !validTextAttachmentMediaType(attachment.mediaType) &&
       !attachmentHasSafeImagePreview(attachment))
    {
      QString canonicalPath;
      QFileInfo currentInfo(info.absoluteFilePath());
      if(fileInfoIsSafeReferenceTarget(currentInfo, &canonicalPath) &&
         currentInfo.size() == selectedSize &&
         currentInfo.lastModified().toMSecsSinceEpoch() == selectedModifiedMs)
        systemThumbnailPath = canonicalPath;
    }

    QString error;
    if(addComposerAttachment(attachment, &error))
    {
      addedAny = true;
      if(!systemThumbnailPath.isEmpty())
        requestSystemThumbnail(attachment.id, systemThumbnailPath, selectedSize,
                               selectedModifiedMs,
                               validRasterImageMediaType(attachment.mediaType));
    }
    else
    {
      m_ComposerSystemThumbnails.remove(attachment.id);
      m_ComposerSystemAnimations.remove(attachment.id);
      if(!error.isEmpty())
        appendSystem(error, true);
    }
  }
  return addedAny;
}

bool AIAssistant::addMimeAttachments(const QMimeData *mimeData)
{
  if(!mimeData)
    return false;
  if(mimeData->hasUrls())
  {
    QStringList paths;
    for(const QUrl &url : mimeData->urls())
    {
      const QString path = url.toLocalFile();
      if(!path.isEmpty())
        paths.push_back(path);
    }
    if(!paths.isEmpty())
    {
      addAttachmentFiles(paths);
      return true;
    }
  }
  QStringList rawImageFormats;
  for(const QString &format : mimeData->formats())
    if(validRasterImageMediaType(format))
      rawImageFormats.push_back(format);
  std::stable_sort(rawImageFormats.begin(), rawImageFormats.end(), [](const QString &left,
                                                                     const QString &right) {
    const bool leftGIF = attachmentMediaTypeEssence(left) == QLatin1String("image/gif");
    const bool rightGIF = attachmentMediaTypeEssence(right) == QLatin1String("image/gif");
    return leftGIF && !rightGIF;
  });

  bool rawImageTooLarge = false;
  bool rawAnimatedImageTooLarge = false;
  for(const QString &format : rawImageFormats)
  {
    const QByteArray bytes = mimeData->data(format);
    if(bytes.size() > MaxBinaryAttachmentBytes)
    {
      rawImageTooLarge = true;
      const QString mediaType = attachmentMediaTypeEssence(format);
      rawAnimatedImageTooLarge =
          rawAnimatedImageTooLarge || mediaType == QLatin1String("image/gif") ||
          mediaType == QLatin1String("image/webp") ||
          mediaType == QLatin1String("image/x-mng");
      continue;
    }
    QString detectedMediaType;
    QByteArray detectedFormat;
    const bool qtRaster =
        rasterImageBytesHeaderIsSafe(bytes, NULL, &detectedMediaType, &detectedFormat);
    if(!qtRaster)
    {
      detectedMediaType = attachmentMediaTypeEssence(format);
      if(!validRasterImageMediaType(detectedMediaType))
        continue;
    }

    AIChatAttachment attachment;
    attachment.id = lit("attachment-") + newTaskId();    // i18n-ignore: stable local identifier
    QString extension = qtRaster ? QString::fromLatin1(detectedFormat.left(16))
                                 : detectedMediaType.section(QLatin1Char('/'), 1, 1);
    if(extension.startsWith(QLatin1String("x-")))
      extension.remove(0, 2);
    static const QRegularExpression safeExtension(
        lit("^[a-zA-Z0-9]{1,16}$"));    // i18n-ignore: safe file extension validation
    if(!safeExtension.match(extension).hasMatch())
      extension = lit("img");    // i18n-ignore: generic image file extension
    if(extension == QLatin1String("jpeg"))
      extension = lit("jpg");    // i18n-ignore: conventional file extension
    attachment.title = tr("Pasted image.%1").arg(extension);
    attachment.mediaType = detectedMediaType;
    // i18n-ignore: ReplayLab attachment kind IDs selected by protocol media capabilities
    attachment.kind = qtRaster && providerNativeImageMediaType(attachment.mediaType)
                           ? lit("image")    // i18n-ignore: ReplayLab attachment kind ID
                          : lit("file");    // i18n-ignore: ReplayLab attachment kind IDs
    attachment.source = tr("Clipboard");
    attachment.encoding = lit("base64");    // i18n-ignore: wire encoding identifier
    attachment.content = QString::fromLatin1(bytes.toBase64());
    attachment.originalBytes = bytes.size();
    QString error;
    if(addComposerAttachment(attachment, &error))
    {
      if(!qtRaster)
        requestInlineRasterThumbnail(attachment.id, bytes);
    }
    else if(!error.isEmpty())
    {
      appendSystem(error, true);
    }
    return true;
  }

  if(rawAnimatedImageTooLarge)
  {
    appendSystem(tr("The pasted image exceeds the 8 MiB attachment limit."), true);
    return true;
  }

  if(mimeData->hasImage())
  {
    QImage image = qvariant_cast<QImage>(mimeData->imageData());
    if(image.isNull())
      image = qvariant_cast<QPixmap>(mimeData->imageData()).toImage();
    if(image.isNull() || !image.size().isValid() || image.width() > MaxImageDimension ||
       image.height() > MaxImageDimension ||
       qint64(image.width()) * qint64(image.height()) > MaxImagePixels)
    {
      appendSystem(tr("The pasted image is invalid or its dimensions exceed the safety limit."),
                   true);
      return true;
    }

    QByteArray bytes;
    QBuffer output(&bytes);
    output.open(QIODevice::WriteOnly);
    QString mediaType = lit("image/png");    // i18n-ignore: image MIME type
    bool encoded = image.save(&output, "PNG");    // i18n-ignore: Qt image codec name
    if((!encoded || bytes.size() > MaxBinaryAttachmentBytes) && !image.hasAlphaChannel())
    {
      bytes.clear();
      output.close();
      output.open(QIODevice::WriteOnly);
      mediaType = lit("image/jpeg");    // i18n-ignore: image MIME type
      encoded = image.save(&output, "JPEG", 90);    // i18n-ignore: Qt image codec name
    }
    output.close();
    if(!encoded || bytes.isEmpty() || bytes.size() > MaxBinaryAttachmentBytes)
    {
      appendSystem(tr("The pasted image exceeds the 8 MiB attachment limit."), true);
      return true;
    }

    AIChatAttachment attachment;
    attachment.id = lit("attachment-") + newTaskId();    // i18n-ignore: stable local identifier
    attachment.kind = lit("image");    // i18n-ignore: ReplayLab attachment kind
    attachment.title = mediaType == QLatin1String("image/jpeg") ? tr("Pasted image.jpg")
                                                                 : tr("Pasted image.png");
    attachment.mediaType = mediaType;
    attachment.source = tr("Clipboard");
    attachment.encoding = lit("base64");    // i18n-ignore: wire encoding identifier
    attachment.content = QString::fromLatin1(bytes.toBase64());
    attachment.originalBytes = bytes.size();
    QString error;
    if(!addComposerAttachment(attachment, &error) && !error.isEmpty())
      appendSystem(error, true);
    return true;
  }
  if(!rawImageFormats.isEmpty())
  {
    appendSystem(rawImageTooLarge
                     ? tr("The pasted image exceeds the 8 MiB attachment limit.")
                     : tr("The pasted image is invalid or its dimensions exceed the safety limit."),
                 true);
    return true;
  }
  return false;
}

void AIAssistant::removeComposerAttachment(const QString &attachmentId)
{
  for(int i = 0; i < m_ComposerAttachments.size(); ++i)
  {
    if(m_ComposerAttachments[i].id == attachmentId)
    {
      const QString pair = m_ComposerAttachments[i]
                               .context.value(QLatin1String("annotation"))
                               .toObject()
                               .value(QLatin1String("pairId"))
                               .toString();
      for(int j = m_ComposerAttachments.size() - 1; j >= 0; --j)
      {
        const AIChatAttachment &item = m_ComposerAttachments[j];
        if(item.id != attachmentId &&
           (pair.isEmpty() || item.context.value(QLatin1String("annotation"))
                                      .toObject()
                                      .value(QLatin1String("pairId"))
                                      .toString() != pair))
          continue;
        m_ComposerSystemThumbnails.remove(item.id);
        m_ComposerSystemAnimations.remove(item.id);
        m_ComposerAttachments.remove(j);
      }
      rebuildAttachmentChips();
      return;
    }
  }
}

void AIAssistant::previewComposerAttachment(const QString &attachmentId)
{
  const AIChatAttachment *selected = NULL;
  for(const AIChatAttachment &attachment : m_ComposerAttachments)
    if(attachment.id == attachmentId)
      selected = &attachment;
  if(!selected)
    return;
  if(!selected->annotationPairId().isEmpty())
  {
    editRenderTargetAnnotation(attachmentId);
    return;
  }

  QDialog &dialog = *new QDialog(this);
  dialog.setAttribute(Qt::WA_DeleteOnClose);
  dialog.setWindowTitle(tr("Attachment preview"));
  dialog.resize(900, 680);
  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QLabel *title = new QLabel(selected->title, &dialog);
  title->setTextFormat(Qt::PlainText);
  QFont titleFont = title->font();
  titleFont.setBold(true);
  title->setFont(titleFont);
  layout->addWidget(title);
  if(!selected->source.isEmpty())
  {
    QLabel *source = new QLabel(tr("Source: %1").arg(selected->source), &dialog);
    source->setTextFormat(Qt::PlainText);
    source->setTextInteractionFlags(Qt::TextSelectableByMouse);
    source->setWordWrap(true);
    layout->addWidget(source);
  }
  QLabel *trust = new QLabel(
      tr("Reference material is treated as data, not as instructions."), &dialog);
  trust->setTextFormat(Qt::PlainText);
  trust->setWordWrap(true);
  layout->addWidget(trust);
  QLabel *metadata = new QLabel(
      tr("Type: %1\nSize: %2 bytes").arg(selected->mediaType).arg(selected->originalBytes),
      &dialog);
  metadata->setTextFormat(Qt::PlainText);
  metadata->setTextInteractionFlags(Qt::TextSelectableByMouse);
  layout->addWidget(metadata);

  auto addImagePreview = [this, &dialog, layout](const AIChatAttachment &attachment) {
    QSize sourceSize;
    if(!attachmentImageSize(attachment, &sourceSize))
    {
      QLabel *failure = new QLabel(tr("The image preview could not be decoded safely."), &dialog);
      failure->setTextFormat(Qt::PlainText);
      failure->setAlignment(Qt::AlignCenter);
      layout->addWidget(failure, 1);
      return;
    }

    AttachmentImagePreview *preview = new AttachmentImagePreview(&dialog);
    preview->setToolTip(tr("Use the mouse wheel to zoom and drag to move the image."));
    preview->setAccessibleDescription(
        tr("Image preview. Use the mouse wheel to zoom and drag to move the image."));
    QMovie *movie = safeAttachmentMovie(attachment, sourceSize, preview);
    if(movie)
    {
      preview->setMovie(movie, sourceSize);
    }
    else
    {
      const QPixmap pixmap = attachmentPreviewPixmap(attachment);
      if(pixmap.isNull())
      {
        delete preview;
        QLabel *failure =
            new QLabel(tr("The image preview could not be decoded safely."), &dialog);
        failure->setTextFormat(Qt::PlainText);
        failure->setAlignment(Qt::AlignCenter);
        layout->addWidget(failure, 1);
        return;
      }
      preview->setPixmap(pixmap);
    }
    layout->addWidget(preview, 1);
  };

  auto addPixmapPreview = [this, &dialog, layout](const QPixmap &pixmap) {
    if(pixmap.isNull())
      return false;
    AttachmentImagePreview *preview = new AttachmentImagePreview(&dialog);
    preview->setToolTip(tr("Use the mouse wheel to zoom and drag to move the image."));
    preview->setAccessibleDescription(
        tr("Image preview. Use the mouse wheel to zoom and drag to move the image."));
    preview->setPixmap(pixmap);
    layout->addWidget(preview, 1);
    return true;
  };

  auto addTextPreview = [&dialog, layout](const QString &text) {
    if(text.isEmpty())
      return false;
    QPlainTextEdit *contents = new QPlainTextEdit(&dialog);
    contents->setReadOnly(true);
    contents->setPlainText(text);
    layout->addWidget(contents, 1);
    return true;
  };

  auto addSystemRasterPreview =
      [this, &dialog, layout](const AIChatAttachment &attachment,
                             const QImage &fallbackThumbnail) {
        if(!attachmentCanUseSystemRasterDecoder(attachment))
          return false;

        AttachmentImagePreview *preview = new AttachmentImagePreview(&dialog);
        preview->setToolTip(tr("Use the mouse wheel to zoom and drag to move the image."));
        preview->setAccessibleDescription(
            tr("Image preview. Use the mouse wheel to zoom and drag to move the image."));
        preview->hide();
        layout->addWidget(preview, 1);

        QLabel *loading = new QLabel(tr("Loading image preview..."), &dialog);
        loading->setTextFormat(Qt::PlainText);
        loading->setAlignment(Qt::AlignCenter);
        loading->setWordWrap(true);
        layout->addWidget(loading, 1);

        QPointer<AIAssistant> assistant(this);
        QPointer<QDialog> dialogGuard(&dialog);
        QPointer<AttachmentImagePreview> previewGuard(preview);
        QPointer<QLabel> loadingGuard(loading);
        aiBackgroundThreadPool()->start(new FunctionRunnable(
            [assistant, dialogGuard, previewGuard, loadingGuard, attachment,
             fallbackThumbnail]() {
              const AIFileThumbnail::RasterAnimationResult animation =
                  attachmentSystemRasterAnimation(attachment);
              AIFileThumbnail::RasterDecodeResult decoded;
              if(animation.isNull())
                decoded = attachmentSystemRasterPreview(attachment);
              if(!assistant)
                return;
              QTimer::singleShot(
                  0, assistant.data(),
                  [dialogGuard, previewGuard, loadingGuard, attachment, decoded, animation,
                   fallbackThumbnail]() {
                    if(!dialogGuard || !previewGuard || !loadingGuard)
                      return;

                    const bool pathValid = !isPathReferenceAttachment(attachment) ||
                                           pathReferenceMatchesCurrentFile(attachment);
                    if(pathValid && !animation.isNull())
                    {
                      const QSize sourceSize = animation.sourceSize;
                      QPointer<AttachmentImagePreview> animatedPreview(previewGuard);
                      AttachmentFrameAnimation *player = new AttachmentFrameAnimation(
                          animation.frames, animation.frameDelaysMs, previewGuard.data(),
                          [animatedPreview, sourceSize](const QImage &frame, bool firstFrame) {
                            if(animatedPreview)
                              animatedPreview->setRasterAnimationFrame(frame, sourceSize,
                                                                       firstFrame);
                          });
                      loadingGuard->hide();
                      previewGuard->show();
                      player->start();
                      return;
                    }

                    QImage image = decoded.image;
                    if(!pathValid)
                      image = QImage();
                    if(image.isNull() && pathValid)
                      image = fallbackThumbnail;

                    const QPixmap pixmap = QPixmap::fromImage(std::move(image));
                    if(pixmap.isNull())
                    {
                      loadingGuard->setText(
                          QCoreApplication::translate(
                              "AIAssistant",
                              "The image preview could not be decoded safely."));
                      return;
                    }

                    previewGuard->setPixmap(pixmap);
                    loadingGuard->hide();
                    previewGuard->show();
                  });
            }), -1);
        return true;
      };

  QString textPreview;
  const bool hasTextPreview =
      attachmentTextPreview(*selected, &textPreview, MaxAttachmentContentChars);
  const QImage systemThumbnail = m_ComposerSystemThumbnails.value(selected->id);

  if(isInlineRasterAttachment(*selected) && attachmentHasSafeImagePreview(*selected))
  {
    addImagePreview(*selected);
  }
  else if(selected->kind == QLatin1String("file"))
  {
    if(addSystemRasterPreview(*selected, systemThumbnail))
    {
    }
    else if(!hasTextPreview && !addPixmapPreview(QPixmap::fromImage(systemThumbnail)))
    {
      QLabel *binaryNotice =
          new QLabel(tr("Binary file contents are not rendered in the preview."), &dialog);
      binaryNotice->setTextFormat(Qt::PlainText);
      binaryNotice->setAlignment(Qt::AlignCenter);
      binaryNotice->setWordWrap(true);
      layout->addWidget(binaryNotice, 1);
    }
    else if(hasTextPreview)
    {
      addTextPreview(textPreview);
    }
  }
  else if(isPathReferenceAttachment(*selected))
  {
    QLabel *pathLabel = new QLabel(tr("Referenced path"), &dialog);
    pathLabel->setTextFormat(Qt::PlainText);
    pathLabel->setWordWrap(true);
    layout->addWidget(pathLabel);

    QLineEdit *pathEdit = new QLineEdit(&dialog);
    pathEdit->setText(selected->content);
    pathEdit->setReadOnly(true);
    pathEdit->setToolTip(tr("Select or copy the referenced path"));
    pathEdit->setAccessibleName(tr("Referenced file path"));
    layout->addWidget(pathEdit);

    QPushButton *copyPath = new QPushButton(tr("Copy path"), &dialog);
    copyPath->setToolTip(tr("Copy the referenced path to the clipboard"));
    connect(copyPath, &QPushButton::clicked, &dialog, [pathEdit]() {
      if(QApplication::clipboard())
        QApplication::clipboard()->setText(pathEdit->text());
      pathEdit->selectAll();
    });
    layout->addWidget(copyPath, 0, Qt::AlignLeft);

    if(attachmentHasSafeImagePreview(*selected))
      addImagePreview(*selected);
    else if(addSystemRasterPreview(*selected, systemThumbnail))
    {
    }
    else if(hasTextPreview)
      addTextPreview(textPreview);
    else if(!addPixmapPreview(QPixmap::fromImage(systemThumbnail)))
    {
      QLabel *binaryNotice =
          new QLabel(tr("Binary file contents are not rendered in the preview."), &dialog);
      binaryNotice->setTextFormat(Qt::PlainText);
      binaryNotice->setAlignment(Qt::AlignCenter);
      binaryNotice->setWordWrap(true);
      layout->addWidget(binaryNotice, 1);
    }
  }
  else
  {
    addTextPreview(selected->content);
  }
  QDialogButtonBox *buttons = new QDialogButtonBox(QDialogButtonBox::Close, &dialog);
  connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  layout->addWidget(buttons);
  dialog.show();
}

void AIAssistant::rebuildAttachmentChips()
{
  if(!m_AttachmentLayout || !m_AttachmentScroll)
    return;
  while(QLayoutItem *item = m_AttachmentLayout->takeAt(0))
  {
    if(item->widget())
      item->widget()->deleteLater();
    delete item;
  }

  for(const AIChatAttachment &attachment : m_ComposerAttachments)
  {
    if(AIHideAnnotationOriginal(attachment, m_ComposerAttachments))
      continue;
    const QSize thumbnailSize(54, 54);
    QPixmap visualThumbnail = attachmentThumbnail(attachment, thumbnailSize);
    if(visualThumbnail.isNull())
    {
      QString textPreview;
      if(attachmentTextPreview(attachment, &textPreview))
        visualThumbnail = textAttachmentThumbnail(textPreview, thumbnailSize, palette());
    }
    if(visualThumbnail.isNull())
    {
      const QImage systemThumbnail = m_ComposerSystemThumbnails.value(attachment.id);
      if(!systemThumbnail.isNull())
        visualThumbnail = QPixmap::fromImage(systemThumbnail).scaled(
            thumbnailSize, Qt::KeepAspectRatio, Qt::SmoothTransformation);
    }
    const bool hasVisualThumbnail = !visualThumbnail.isNull();

    QFrame *chip = new QFrame(m_AttachmentStrip);
    chip->setObjectName(lit("aiAttachmentChip"));    // i18n-ignore: stylesheet object name
    if(!attachment.annotationPairId().isEmpty())
    {
      chip->setContextMenuPolicy(Qt::CustomContextMenu);
      connect(chip, &QWidget::customContextMenuRequested, this,
              [this, chip, attachment](QPoint point) {
                QMenu menu(chip);
                QAction *edit = menu.addAction(tr("Edit marks"));
                connect(edit, &QAction::triggered, this,
                        [this, attachment]() { editRenderTargetAnnotation(attachment.id); });
                menu.exec(chip->mapToGlobal(point));
              });
    }
    chip->setStyleSheet(
        // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
        lit("QFrame#aiAttachmentChip { border: 1px solid palette(mid); border-radius: 14px; "
            "background: palette(alternate-base); }"));
    QHBoxLayout *chipLayout = new QHBoxLayout(chip);
    chipLayout->setContentsMargins(hasVisualThumbnail ? 3 : 7, 2, 3, 2);
    chipLayout->setSpacing(2);

    if(hasVisualThumbnail)
    {
      QToolButton *preview = new QToolButton(chip);
      preview->setAutoRaise(true);
      preview->setToolButtonStyle(Qt::ToolButtonIconOnly);
      preview->setFixedSize(58, 58);
      preview->setIconSize(thumbnailSize);
      preview->setIcon(QIcon(visualThumbnail));
      preview->setToolTip(tr("Preview attachment: %1").arg(attachment.title));
      preview->setAccessibleName(tr("Preview attachment: %1").arg(attachment.title));
      connect(preview, &QToolButton::clicked, this,
              [this, attachment]() { previewComposerAttachment(attachment.id); });

      QMovie *movie = safeAttachmentMovie(attachment, thumbnailSize, preview);
      if(movie)
      {
        connect(movie, &QMovie::frameChanged, preview, [movie, preview](int) {
          preview->setIcon(QIcon(movie->currentPixmap()));
        });
        movie->start();
      }
      else
      {
        const ComposerRasterAnimation cachedAnimation =
            m_ComposerSystemAnimations.value(attachment.id);
        if(cachedAnimation.isValid())
        {
          QPointer<QToolButton> previewGuard(preview);
          AttachmentFrameAnimation *player = new AttachmentFrameAnimation(
              cachedAnimation.frames, cachedAnimation.frameDelaysMs, preview,
              [previewGuard, thumbnailSize](const QImage &frame, bool) {
                if(!previewGuard || frame.isNull())
                  return;
                const QPixmap pixmap = QPixmap::fromImage(frame).scaled(
                    thumbnailSize, Qt::KeepAspectRatio, Qt::SmoothTransformation);
                previewGuard->setIcon(QIcon(pixmap));
              });
          player->start();
        }
      }
      chipLayout->addWidget(preview);
    }
    else
    {
      QLabel *type = new QLabel(chip);
      const bool fileAttachment = attachment.kind == QLatin1String("file") ||
                                  isPathReferenceAttachment(attachment);
      type->setText(fileAttachment ? tr("File") : tr("Text"));
      type->setAccessibleName(fileAttachment ? tr("File attachment")
                                             : tr("Text attachment"));
      chipLayout->addWidget(type);

      QPushButton *preview = new QPushButton(attachment.title, chip);
      preview->setIcon(fileAttachment ? Icons::folder_page_white() : Icons::page_white_code());
      preview->setFlat(true);
      preview->setMaximumWidth(280);
      preview->setToolTip(tr("Preview attachment: %1").arg(attachment.title));
      preview->setAccessibleName(tr("Preview attachment: %1").arg(attachment.title));
      connect(preview, &QPushButton::clicked, this,
              [this, attachment]() { previewComposerAttachment(attachment.id); });
      chipLayout->addWidget(preview);
    }

    QToolButton *remove = new QToolButton(chip);
    remove->setAutoRaise(true);
    remove->setIcon(Icons::cross());
    remove->setToolTip(tr("Remove attachment: %1").arg(attachment.title));
    remove->setAccessibleName(tr("Remove attachment: %1").arg(attachment.title));
    connect(remove, &QToolButton::clicked, this,
            [this, attachment]() { removeComposerAttachment(attachment.id); });
    chipLayout->addWidget(remove);
    m_AttachmentLayout->addWidget(chip);
  }
  m_AttachmentLayout->addStretch(1);
  m_AttachmentScroll->setVisible(!m_ComposerAttachments.isEmpty());
}

void AIAssistant::appendSystem(const QString &text, bool error, const QString &conversationId)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return;
  AIChatMessage item;
  item.id = newMessageId();
  item.role = lit("system");
  item.text = text;
  item.state = error ? lit("error") : lit("status");
  item.timestampMs = QDateTime::currentMSecsSinceEpoch();
  thread->messages.push_back(item);
  thread->updatedMs = item.timestampMs;
  if(thread->id == m_ActiveConversationId)
    thread->unread = false;
  renderConversation();
}

QString AIAssistant::appendUser(const QString &text, const QString &taskId, const QString &state,
                                const QString &conversationId,
                                const QVector<AIChatAttachment> &attachments)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return QString();
  // Any accepted user turn resolves the currently displayed clarification. The prompt still
  // travels through startChat/StartCaptureIntent so its RenderDoc context is frozen normally.
  clearClarification(thread->id);
  AIChatMessage item;
  item.id = newMessageId();
  item.role = lit("user");
  item.text = text;
  item.taskId = taskId;
  item.state = state.isEmpty() ? lit("queued") : state;
  item.attachments = attachments;
  item.timestampMs = QDateTime::currentMSecsSinceEpoch();
  item.activityExpanded = item.state == lit("queued") || item.state == lit("running");
  thread->messages.push_back(item);
  thread->updatedMs = item.timestampMs;
  if(thread->id == m_ActiveConversationId)
    thread->unread = false;
  if(thread->title == tr("New conversation") || thread->title.trimmed().isEmpty())
  {
    QString title = text.simplified();
    if(title.size() > 52)
      title = title.left(49) + lit("...");
    if(!title.isEmpty())
      thread->title = title;
  }
  renderConversation(true);
  scheduleConversationSave();
  return item.id;
}

void AIAssistant::appendAssistant(const QString &text, const QString &conversationId,
                                  const QString &taskId, const QJsonArray &artifacts)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return;
  AIChatMessage item;
  item.id = newMessageId();
  item.role = lit("assistant");
  item.text = text;
  item.taskId = taskId;
  item.artifacts = artifacts;
  if(safeTaskId(taskId))
  {
    QFile stateFile(QDir(taskEventRoot()).filePath(taskId + QLatin1String(".report-state.json")));
    if(stateFile.size() <= 2 * 1024 * 1024 && stateFile.open(QIODevice::ReadOnly))
    {
      const QJsonObject state = QJsonDocument::fromJson(stateFile.readAll()).object();
      if(state.value(QLatin1String("schema")).toString() ==
             QLatin1String("replaylab-task-report-state/v1") &&
         state.value(QLatin1String("taskId")).toString() == taskId &&
         state.value(QLatin1String("conversationId")).toString() == thread->id)
      {
        const QJsonArray refs = state.value(QLatin1String("replyVisuals")).toArray();
        for(int i = 0; i < qMin(8, refs.size()); ++i)
        {
          const QString id = refs[i].toObject().value(QLatin1String("id")).toString();
          if(AIVisualReply::validIdentity(id))
            item.visuals.append(QJsonObject{{QLatin1String("id"), id}});
        }
      }
    }
  }
  item.state = lit("completed");
  item.timestampMs = QDateTime::currentMSecsSinceEpoch();
  item.finishedTimestampMs = item.timestampMs;
  thread->messages.push_back(item);
  thread->updatedMs = item.timestampMs;
  thread->unread = thread->id != m_ActiveConversationId;
  renderConversation(true);
  scheduleConversationSave();
}

AIAssistant::ConversationThread *AIAssistant::conversation(const QString &conversationId)
{
  const QString selected = conversationId.isEmpty() ? m_ActiveConversationId : conversationId;
  for(ConversationThread &thread : m_Conversations)
    if(thread.id == selected)
      return &thread;
  return NULL;
}

const AIAssistant::ConversationThread *AIAssistant::conversation(
    const QString &conversationId) const
{
  const QString selected = conversationId.isEmpty() ? m_ActiveConversationId : conversationId;
  for(const ConversationThread &thread : m_Conversations)
    if(thread.id == selected)
      return &thread;
  return NULL;
}

AIChatMessage *AIAssistant::message(const QString &conversationId, const QString &messageId)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return NULL;
  for(AIChatMessage &item : thread->messages)
    if(item.id == messageId)
      return &item;
  return NULL;
}

AIChatMessage *AIAssistant::taskMessage(const QString &taskId)
{
  return message(m_TaskConversationIds.value(taskId), m_TaskMessageIds.value(taskId));
}

void AIAssistant::renderConversation(bool refreshList)
{
  if(m_ConversationRenderTimer && m_ConversationRenderTimer->isActive())
    m_ConversationRenderTimer->stop();
  if(refreshList)
    refreshConversationList();
  const ConversationThread *thread = conversation();
  if(m_Transcript)
  {
    if(thread)
      m_Transcript->setMessages(thread->messages);
    else
      m_Transcript->clear();
  }
  updateComposerState();
}

void AIAssistant::scheduleConversationRender()
{
  if(!m_ConversationRenderTimer)
  {
    renderConversation();
    return;
  }

  if(!m_ConversationRenderTimer->isActive())
  {
    // Structural updates still lay out a complete document. Nested activity tables are expensive
    // even when their text is short, so account for rows as well as text size. Live text and
    // elapsed labels have their own in-place timers and do not need structural rebuilds.
    qint64 contentChars = 0;
    int activityRows = 0;
    const ConversationThread *thread = conversation();
    if(thread)
    {
      for(const AIChatMessage &message : thread->messages)
      {
        contentChars += message.text.size() + message.provisionalText.size();
        if(message.activityExpanded)
          activityRows += message.activity.size();
        for(const AIActivityItem &activity : message.activity)
          contentChars += activity.title.size() + activity.detail.size();
        if(contentChars >= 512 * 1024)
          break;
      }
    }

    const int intervalMs = contentChars < 64 * 1024 && activityRows < 64     ? 60
                           : contentChars < 512 * 1024 && activityRows < 256 ? 250
                                                                            : 750;
    m_ConversationRenderTimer->setInterval(intervalMs);
    m_ConversationRenderTimer->start();
  }
}

void AIAssistant::refreshConversationList()
{
  if(!m_ConversationList)
    return;

  QSet<QString> runningConversationIds;
  for(auto it = m_TaskConversationIds.cbegin(); it != m_TaskConversationIds.cend(); ++it)
    if(m_RunningTasks.contains(it.key()))
      runningConversationIds.insert(it.value());

  QVector<const ConversationThread *> ordered;
  ordered.reserve(m_Conversations.size());
  for(const ConversationThread &thread : m_Conversations)
    ordered.push_back(&thread);
  std::stable_sort(ordered.begin(), ordered.end(),
                   [&runningConversationIds,
                    this](const ConversationThread *a, const ConversationThread *b) {
                     if(a->pinned != b->pinned)
                       return a->pinned;
                     const bool aRunning = runningConversationIds.contains(a->id);
                     const bool bRunning = runningConversationIds.contains(b->id);
                     if(aRunning != bRunning)
                       return aRunning;
                     const bool aUnread = a->unread && a->id != m_ActiveConversationId;
                     const bool bUnread = b->unread && b->id != m_ActiveConversationId;
                     if(aUnread != bUnread)
                       return aUnread;
                     if(a->updatedMs != b->updatedMs)
                       return a->updatedMs > b->updatedMs;
                     if(a->createdMs != b->createdMs)
                       return a->createdMs > b->createdMs;
                     return a->id < b->id;
                   });

  QVector<AIConversationListItem> items;
  items.reserve(ordered.size());
  for(const ConversationThread *thread : ordered)
  {
    AIConversationListItem item;
    item.id = thread->id;
    item.title = thread->title;
    item.pinned = thread->pinned;
    item.unread = thread->unread && thread->id != m_ActiveConversationId;
    item.running = runningConversationIds.contains(thread->id);
    items.push_back(item);
  }
  m_ConversationList->setActiveId(m_ActiveConversationId);
  m_ConversationList->setItems(items);
}

void AIAssistant::refreshPendingChats()
{
  if(!m_PendingChats)
    return;
  QVector<AIPendingChatItem> items;
  for(const PendingChat &request : m_ChatQueue)
  {
    if(request.conversationId != m_ActiveConversationId)
      continue;
    AIPendingChatItem item;
    item.id = request.taskId;
    item.text = request.prompt;
    const ChatExecution *execution = conversationExecution(request.conversationId);
    item.canAdjust = execution &&
                     !m_CompletedTasks.contains(execution->request.taskId) &&
                     (!request.contextFreezeRequired || !request.contextSnapshot.isEmpty());
    items.push_back(item);
  }
  m_PendingChats->setItems(items);
}

void AIAssistant::resetConversations()
{
  m_Conversations.clear();
  m_ComposerAttachments.clear();
  m_ComposerSystemThumbnails.clear();
  m_ComposerSystemAnimations.clear();
  rebuildAttachmentChips();
  ConversationThread thread;
  thread.id = lit("conversation-") + newTaskId();
  thread.title = tr("New conversation");
  thread.profileId = m_ActiveProfile;
  thread.modelId = m_ActiveModel;
  thread.createdMs = thread.updatedMs = QDateTime::currentMSecsSinceEpoch();
  m_Conversations.push_back(thread);
  m_ActiveConversationId = thread.id;
  refreshConversationList();
  renderConversation();
}

void AIAssistant::createConversation()
{
  if(m_Conversations.size() >= MaxConversationThreads)
  {
    QSet<QString> protectedIds;
    protectedIds.insert(m_ActiveConversationId);
    for(auto it = m_ChatExecutions.cbegin(); it != m_ChatExecutions.cend(); ++it)
      if(it.value())
        protectedIds.insert(it.value()->request.conversationId);
    for(auto it = m_TaskConversationIds.cbegin(); it != m_TaskConversationIds.cend(); ++it)
      protectedIds.insert(it.value());
    for(const PendingChat &queued : m_ChatQueue)
      protectedIds.insert(queued.conversationId);

    int removable = -1;
    for(int i = 0; i < m_Conversations.size(); ++i)
    {
      if(m_Conversations[i].messages.isEmpty() && !m_Conversations[i].pinned &&
         !protectedIds.contains(m_Conversations[i].id))
      {
        removable = i;
        break;
      }
    }
    if(removable < 0)
    {
      appendSystem(tr("This capture already has %1 conversations. Delete one before creating "
                      "another.")
                       .arg(MaxConversationThreads),
                   true);
      return;
    }
    m_Conversations.remove(removable);
  }

  ConversationThread thread;
  thread.id = lit("conversation-") + newTaskId();
  thread.title = tr("New conversation");
  thread.profileId = m_ActiveProfile;
  thread.modelId = m_ActiveModel;
  thread.createdMs = thread.updatedMs = QDateTime::currentMSecsSinceEpoch();
  m_Conversations.push_back(thread);
  selectConversation(thread.id);
}

void AIAssistant::deleteConversation(const QString &conversationId)
{
  const QString targetId = conversationId.isEmpty() ? m_ActiveConversationId : conversationId;
  if(targetId.isEmpty() || !conversation(targetId))
    return;
  if(QMessageBox::question(this, tr("Delete conversation"),
                           tr("Delete this conversation, its task logs and temporary files? "
                              "Active requests will be stopped. Saved captures and project files "
                              "will be kept.")) !=
     QMessageBox::Yes)
    return;
  // QMessageBox runs a nested event loop. A queued turn can start while the confirmation is
  // visible, so collect the target's tasks only after confirmation.
  const ConversationThread *target = conversation(targetId);
  if(!target)
    return;
  QSet<QString> taskIds;
  // Completed tasks have already left m_TaskConversationIds. Their stored messages retain
  // ownership, and the cleanup command also finds older tasks omitted from bounded history.
  for(const AIChatMessage &item : target->messages)
    if(safeTaskId(item.taskId))
      taskIds.insert(item.taskId);
  for(auto it = m_TaskConversationIds.cbegin(); it != m_TaskConversationIds.cend(); ++it)
    if(it.value() == targetId && safeTaskId(it.key()))
      taskIds.insert(it.key());
  for(auto it = m_ChatExecutions.cbegin(); it != m_ChatExecutions.cend(); ++it)
    if(it.value() && it.value()->request.conversationId == targetId)
      taskIds.insert(it.key());
  for(int i = m_ChatQueue.size() - 1; i >= 0; --i)
    if(m_ChatQueue[i].conversationId == targetId)
    {
      const QString taskId = m_ChatQueue[i].taskId;
      if(safeTaskId(taskId))
        taskIds.insert(taskId);
    }
  // Persist only opaque IDs before changing either the queue or the transcript. A crash/close
  // between the UI save and background process launch must not lose the authorized cleanup.
  if(!saveConversationCleanupRequest(taskEventRoot(), targetId, taskIds))
  {
    appendSystem(tr("The conversation could not be deleted because its cleanup request could "
                    "not be saved."), true);
    return;
  }
  for(int i = m_ChatQueue.size() - 1; i >= 0; --i)
    if(m_ChatQueue[i].conversationId == targetId)
      m_ChatQueue.remove(i);
  for(const QString &taskId : taskIds)
  {
    // Stop the whole contained process tree before handing its files to cleanup. Do not
    // advance this conversation's queue or block the GUI waiting for process termination.
    cancelChat(lit("cancelled"), false, taskId, 0, false);    // i18n-ignore: internal terminal state
    forgetDeletedTask(taskId);
  }
  for(int i = 0; i < m_Conversations.size(); ++i)
  {
    if(m_Conversations[i].id == targetId)
    {
      m_Conversations.remove(i);
      break;
    }
  }
  if(m_Conversations.isEmpty())
  {
    resetConversations();
  }
  else
  {
    if(m_ActiveConversationId == targetId)
    {
      const ConversationThread *latest = &m_Conversations.front();
      for(const ConversationThread &thread : m_Conversations)
        if(thread.updatedMs > latest->updatedMs)
          latest = &thread;
      m_ActiveConversationId = latest->id;
      syncConnectionSelectionFromConversation();
    }
    refreshConversationList();
    renderConversation();
  }
  updateComposerState();
  advanceChatContextFreeze();
  // Capture an immutable save now, before an RDC switch can change the storage destination.
  saveConversations();
  QTimer::singleShot(0, this, [this]() { startNextChat(); });

  // i18n-ignore: AgentHost maintenance CLI flags, never shown as UI labels.
  QStringList arguments = {lit("conversation-cleanup"), lit("--conversation-id"), targetId,
                           lit("--json")};    // i18n-ignore: AgentHost CLI output-format flag.
  for(const QString &taskId : taskIds)
    arguments << lit("--task-id") << taskId;    // i18n-ignore: AgentHost CLI flag
  runCLIAsync(arguments, QByteArray(), 30000, this,
              [this, targetId](int exitCode, const QByteArray &output, const QString &diagnostic, bool) {
                const QJsonObject result = QJsonDocument::fromJson(output).object();
                // i18n-ignore: Stable cleanup receipt protocol fields.
                const bool matching = result.value(lit("conversationId")).toString() == targetId;
                if(matching)
                  for(const QJsonValue &task : result.value(lit("taskIds")).toArray())    // i18n-ignore: Cleanup receipt task-identity key.
                    if(safeTaskId(task.toString()))
                      forgetDeletedTask(task.toString());
                if(exitCode != 0 || !matching || !result.value(lit("complete")).toBool())    // i18n-ignore: Cleanup receipt completion key.
                  appendSystem(tr("The conversation was deleted, but some task files could not "
                                  "be cleaned up. Saved work was kept. %1")
                                   .arg(diagnostic), true);
              });
}

void AIAssistant::forgetDeletedTask(const QString &taskId)
{
  m_DeletedTasks.insert(taskId);
  unwatchTask(taskId);
  m_RunningTasks.remove(taskId);
  m_CompletedTasks.remove(taskId);
  m_UserPrompts.remove(taskId);
  m_TaskOffsets.remove(taskId);
  m_TaskSequences.remove(taskId);
  m_TaskTails.remove(taskId);
  m_TaskTexts.remove(taskId);
  m_TaskAgentMessageStreams.remove(taskId);
  m_TaskConversationIds.remove(taskId);
  m_TaskMessageIds.remove(taskId);
  if(m_AgentTraceStore)
    m_AgentTraceStore->ForgetTask(taskId);
  if(m_AgentMonitor)
    m_AgentMonitor->ForgetTask(taskId);
}

void AIAssistant::toggleConversationPinned(const QString &conversationId)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return;
  thread->pinned = !thread->pinned;
  refreshConversationList();
  scheduleConversationSave();
}

void AIAssistant::selectConversation(const QString &conversationId)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return;
  if(m_ActiveConversationId != conversationId)
  {
    // Composer attachments belong to the draft in the conversation where they were added. Do
    // not silently carry source snippets or file contents into another conversation.
    m_ComposerAttachments.clear();
    m_ComposerSystemThumbnails.clear();
    m_ComposerSystemAnimations.clear();
    rebuildAttachmentChips();
  }
  m_ActiveConversationId = conversationId;
  syncConnectionSelectionFromConversation();
  thread->unread = false;
  refreshConversationList();
  renderConversation();
  scheduleConversationSave();
}

void AIAssistant::addTaskActivity(const QString &taskId, const QString &kind,
                                  const QString &title, const QString &detail, bool completed,
                                  bool error, qint64 timestampMs, const QString &activityId)
{
  AIChatMessage *item = taskMessage(taskId);
  if(!item)
    return;
  const qint64 eventTimestampMs =
      timestampMs > 0 ? timestampMs : QDateTime::currentMSecsSinceEpoch();
  AIActivityItem activity;
  activity.id = activityId.left(MaxActivityIdChars);
  activity.kind = kind;
  activity.title = title;
  activity.detail = detail;
  activity.timestampMs = eventTimestampMs;
  // An error flag also marks attention/approval states, which can still be live. Only
  // completed activities have a stable end timestamp.
  activity.finishedTimestampMs = completed ? eventTimestampMs : 0;
  activity.completed = completed;
  activity.error = error;
  activity.liveText = isLiveTextActivityKind(kind);
  bool updated = false;
  for(int i = item->activity.size() - 1; i >= 0; --i)
  {
    AIActivityItem &existing = item->activity[i];
    const bool identityMatches =
        !activity.id.isEmpty() ? existing.id == activity.id
                               // i18n-ignore: Activity kind is a backend protocol identifier used for matching.
                               : (activity.kind != lit("tool") ||
                                  existing.title == activity.title);
    if(existing.kind == activity.kind && identityMatches && !existing.completed)
    {
      if(existing.id.isEmpty())
        existing.id = activity.id;
      existing.title = activity.title;
      if(activity.liveText && existing.detail != activity.detail &&
         !activity.detail.startsWith(existing.detail))
        ++existing.liveTextRevision;
      existing.detail = activity.detail;
      existing.liveText = existing.liveText || activity.liveText;
      if(existing.timestampMs <= 0)
        existing.timestampMs = activity.timestampMs;
      existing.finishedTimestampMs = activity.finishedTimestampMs;
      existing.completed = activity.completed;
      existing.error = activity.error;
      updated = true;
      break;
    }
  }
  if(!updated)
  {
    item->activity.push_back(activity);
    if(item->activity.size() > MaxLiveConversationActivities)
    {
      // The Agent Monitor and JSONL remain the complete trace. Keep the ordinary conversation
      // bounded so thousands of lifecycle/tool rows cannot dominate every rich-text layout.
      int removeCount = item->activity.size() - LiveConversationActivityTrimTarget;
      for(int i = 0; i < item->activity.size() && removeCount > 0;)
      {
        if(item->activity[i].completed)
        {
          item->activity.remove(i);
          --removeCount;
        }
        else
        {
          ++i;
        }
      }
      if(removeCount > 0)
        item->activity.remove(0, qMin(removeCount, item->activity.size()));
    }
  }
  item->activityExpanded = true;
  // Several lifecycle/tool events can arrive in one JSONL read. Rebuilding the complete rich-text
  // document for every event blocks the GUI thread and can make qrenderdoc appear frozen.
  scheduleConversationRender();
  scheduleConversationSave();
}

void AIAssistant::appendTaskActivityText(const QString &taskId, const QString &kind,
                                         const QString &title, const QString &text, bool reset,
                                         bool completed, bool error, qint64 timestampMs,
                                         const QString &activityId)
{
  AIChatMessage *item = taskMessage(taskId);
  if(!item)
    return;

  const QString boundedId = activityId.left(MaxActivityIdChars);
  AIActivityItem *activity = NULL;
  for(int i = item->activity.size() - 1; i >= 0; --i)
  {
    AIActivityItem &candidate = item->activity[i];
    // A Codex item can publish its terminal cumulative snapshot immediately before an
    // authoritative reset message. Let that reset replace the same row instead of creating a
    // duplicate completed activity.
    if(candidate.kind == kind && candidate.id == boundedId &&
       (!candidate.completed || reset || completed))
    {
      activity = &candidate;
      break;
    }
  }

  if(!activity)
  {
    addTaskActivity(taskId, kind, title, text, completed, error, timestampMs, boundedId);
    for(int i = item->activity.size() - 1; i >= 0; --i)
    {
      if(item->activity[i].kind == kind && item->activity[i].id == boundedId)
      {
        item->activity[i].exactText = true;
        break;
      }
    }
    return;
  }

  if(reset)
  {
    activity->detail.clear();
    ++activity->liveTextRevision;
  }
  activity->detail += text;
  activity->title = title;
  activity->liveText = true;
  activity->exactText = true;
  if(activity->timestampMs <= 0)
    activity->timestampMs = timestampMs > 0 ? timestampMs : QDateTime::currentMSecsSinceEpoch();
  activity->finishedTimestampMs = completed
                                      ? (timestampMs > 0 ? timestampMs
                                                         : QDateTime::currentMSecsSinceEpoch())
                                      : 0;
  activity->completed = completed;
  activity->error = error;
  item->activityExpanded = true;
  scheduleConversationRender();
  scheduleConversationSave();
}

bool AIAssistant::updateTaskActivityLifecycle(const QString &taskId, const QString &kind,
                                              const QString &title, bool completed, bool error,
                                              qint64 timestampMs, const QString &activityId)
{
  AIChatMessage *item = taskMessage(taskId);
  if(!item)
    return false;

  const QString boundedId = activityId.left(MaxActivityIdChars);
  for(int i = item->activity.size() - 1; i >= 0; --i)
  {
    AIActivityItem &activity = item->activity[i];
    if(activity.kind != kind || activity.id != boundedId)
      continue;

    if(!title.isEmpty())
      activity.title = title;
    if(!activity.completed)
    {
      activity.completed = completed;
      activity.error = error;
      if(completed)
        activity.finishedTimestampMs = timestampMs > 0 ? timestampMs
                                                       : QDateTime::currentMSecsSinceEpoch();
    }
    item->activityExpanded = true;
    scheduleConversationRender();
    scheduleConversationSave();
    return true;
  }
  return false;
}

void AIAssistant::setTaskState(const QString &taskId, const QString &state,
                               bool collapseActivity)
{
  AIChatMessage *item = taskMessage(taskId);
  if(!item)
    return;
  item->state = state;
  const qint64 terminalTimestampMs = QDateTime::currentMSecsSinceEpoch();
  bool hasStreamedActivityText = false;
  for(AIActivityItem &activity : item->activity)
  {
    if(collapseActivity && activity.liveText && !activity.completed)
    {
      // Planning/structured provider observations may have no item-level terminal delta. Once the
      // enclosing task is terminal, close their exact live rows without replacing their text.
      activity.completed = true;
      activity.finishedTimestampMs = terminalTimestampMs;
    }
    // Raw reasoning is context, not the answer: it folds away with the group once the task ends.
    // i18n-ignore: Activity kind is a stable ReplayLab conversation protocol identifier.
    if(activity.kind != lit("reasoning-raw"))
      hasStreamedActivityText |= activity.liveText && !activity.detail.isEmpty();
  }
  item->activityExpanded = !collapseActivity || hasStreamedActivityText;
  if(collapseActivity)
    item->finishedTimestampMs = terminalTimestampMs;
  // i18n-ignore: Task lifecycle state identifiers are used only for control flow.
  else if(state == lit("queued") || state == lit("planning") || state == lit("running") ||
          // i18n-ignore: Task lifecycle state identifier is used only for control flow.
          state == lit("finalizing"))
    item->finishedTimestampMs = 0;
  // Keep a terminal failure/cancellation summary visible. Only successful turns replace their
  // provisional stream with a separate authoritative assistant message.
  if(collapseActivity &&
     // i18n-ignore: Task lifecycle state identifiers used only for control flow.
     (state == lit("completed") || state == lit("partial") ||
      // i18n-ignore: Task lifecycle state identifier used only for control flow.
      state == lit("needs-user-input") ||
      // i18n-ignore: Task lifecycle state identifier used only for control flow.
      state == lit("blocked")))
    item->provisionalText.clear();
  scheduleConversationRender();
  scheduleConversationSave();
}

bool AIAssistant::clarificationFromJSON(const QJsonObject &object, const QString &fallbackId,
                                        ClarificationPrompt &clarification) const
{
  clarification = ClarificationPrompt();
  if(object.isEmpty())
    return false;

  // i18n-ignore: ReplayLab clarification protocol field names and enum values.
  const QJsonValue questionValue = object.value(lit("question"));    // i18n-ignore: clarification protocol key
  const QJsonValue modeValue = object.value(lit("selectionMode"));    // i18n-ignore: clarification protocol key
  const QJsonValue optionsValue = object.value(lit("options"));    // i18n-ignore: clarification protocol key
  const QJsonValue allowOtherValue = object.value(lit("allowOther"));    // i18n-ignore: clarification protocol key
  if(!questionValue.isString() || !modeValue.isString() || !optionsValue.isArray() ||
     !allowOtherValue.isBool())
    return false;

  const QString question = questionValue.toString().trimmed();
  const QString selectionMode = modeValue.toString();
  // i18n-ignore: ReplayLab clarification selection-mode enum values.
  if(question.isEmpty() || question.size() > 2400 ||
     (selectionMode != lit("single") &&    // i18n-ignore: clarification protocol enum
      selectionMode != lit("multiple")))    // i18n-ignore: clarification protocol enum
    return false;

  const QJsonArray optionRows = optionsValue.toArray();
  if(optionRows.size() < 2 || optionRows.size() > 6)
    return false;
  const QRegularExpression safeId(
      lit("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"));    // i18n-ignore: protocol ID validation regex
  QSet<QString> optionIds;
  QVector<ClarificationOption> options;
  options.reserve(optionRows.size());
  for(const QJsonValue &value : optionRows)
  {
    if(!value.isObject())
      return false;
    const QJsonObject row = value.toObject();
    // i18n-ignore: ReplayLab clarification protocol field names.
    if(!row.value(lit("id")).isString() || !row.value(lit("label")).isString())
      return false;
    const QString id = row.value(lit("id")).toString();    // i18n-ignore: clarification protocol key
    const QString label = row.value(lit("label")).toString().simplified();    // i18n-ignore: clarification protocol key
    const QJsonValue descriptionValue = row.value(lit("description"));    // i18n-ignore: clarification protocol key
    if(!descriptionValue.isUndefined() && !descriptionValue.isString())
      return false;
    const QString description = descriptionValue.toString().trimmed();
    if(!safeId.match(id).hasMatch() || optionIds.contains(id) || label.isEmpty() ||
       label.size() > 240 || description.size() > 600)
      return false;
    optionIds.insert(id);
    options.push_back({id, label, description});
  }

  QString id;
  // i18n-ignore: ReplayLab clarification protocol field name.
  const QJsonValue idValue = object.value(lit("id"));
  if(idValue.isString())
    id = idValue.toString();
  else if(!idValue.isUndefined() && !idValue.isNull())
    return false;
  if(id.isEmpty())
    id = lit("clarification-") + fallbackId.left(48);    // i18n-ignore: internal protocol ID prefix
  if(!safeId.match(id).hasMatch())
    return false;

  QString submitLabel;
  // i18n-ignore: ReplayLab clarification protocol field name.
  const QJsonValue submitLabelValue = object.value(lit("submitLabel"));
  if(submitLabelValue.isString())
    submitLabel = submitLabelValue.toString().simplified();
  else if(!submitLabelValue.isUndefined() && !submitLabelValue.isNull())
    return false;
  if(submitLabel.size() > 64)
    return false;

  clarification.id = id;
  clarification.question = question;
  clarification.selectionMode = selectionMode;
  clarification.options = options;
  clarification.allowOther = allowOtherValue.toBool();
  clarification.submitLabel = submitLabel;

  // The value is original user-authored content, not UI source text.
  // i18n-ignore: Persisted ReplayLab clarification protocol field name.
  const QJsonValue originRequestValue = object.value(lit("originRequest"));
  if(originRequestValue.isString())
  {
    const QString originRequest = originRequestValue.toString();
    if(originRequest.size() > MaxClarificationOriginChars)
      return false;
    clarification.originRequest = originRequest;
  }
  else if(!originRequestValue.isUndefined() && !originRequestValue.isNull())
  {
    return false;
  }
  // i18n-ignore: Persisted ReplayLab clarification protocol field name.
  const QJsonValue originRequestSha256Value = object.value(lit("originRequestSha256"));
  if(originRequestSha256Value.isString())
  {
    clarification.originRequestSha256 = originRequestSha256Value.toString();
    if(!clarificationOriginSha256IsValid(clarification.originRequestSha256))
      return false;
  }
  else if(!originRequestSha256Value.isUndefined() && !originRequestSha256Value.isNull())
  {
    return false;
  }
  if(!clarification.originRequest.isEmpty())
  {
    const QString textSha256 = clarificationOriginSha256(clarification.originRequest);
    if(!clarification.originRequestSha256.isEmpty() &&
       clarification.originRequestSha256 != textSha256)
      return false;
    // Upgrade an older persisted short-origin card without breaking schema-v1 readers.
    clarification.originRequestSha256 = textSha256;
  }

  // Persisted native clarifications may carry the exact frozen origin. Backend events normally do
  // not: bindClarificationOrigin() attaches the trusted active request after protocol validation.
  // i18n-ignore: ReplayLab persisted clarification protocol field names.
  const QJsonValue originSnapshotValue = object.value(lit("originContextSnapshot"));
  const QJsonValue originInstanceValue =
      object.value(lit("originContextInstanceId"));    // i18n-ignore: persisted clarification protocol key
  if(!originSnapshotValue.isUndefined() || !originInstanceValue.isUndefined())
  {
    if(!originSnapshotValue.isObject() || !originInstanceValue.isString())
      return false;
    const QByteArray encodedOrigin =
        QJsonDocument(originSnapshotValue.toObject()).toJson(QJsonDocument::Compact);
    const QString originInstance = originInstanceValue.toString();
    if(originInstance.trimmed().isEmpty() || originInstance.size() > 160 ||
       !decodeContextSnapshot(encodedOrigin))
      return false;
    clarification.originContextSnapshot = encodedOrigin;
    clarification.originContextInstanceId = originInstance;
  }
  return clarification.isValid();
}

QJsonObject AIAssistant::clarificationToJSON(
    const ClarificationPrompt &clarification) const
{
  QJsonObject object;
  // i18n-ignore: Persisted ReplayLab clarification protocol field names.
  object[lit("id")] = clarification.id;    // i18n-ignore: clarification protocol key
  object[lit("question")] = clarification.question;    // i18n-ignore: clarification protocol key
  object[lit("selectionMode")] = clarification.selectionMode;    // i18n-ignore: clarification protocol key
  object[lit("allowOther")] = clarification.allowOther;    // i18n-ignore: clarification protocol key
  if(!clarification.submitLabel.isEmpty())
    object[lit("submitLabel")] = clarification.submitLabel;    // i18n-ignore: clarification protocol key
  if(!clarification.originRequest.isEmpty())
    // i18n-ignore: Persisted ReplayLab clarification protocol field. The value is user content.
    object[lit("originRequest")] = clarification.originRequest;
  if(!clarification.originRequestSha256.isEmpty())
    // i18n-ignore: Persisted ReplayLab clarification protocol SHA-256 field.
    object[lit("originRequestSha256")] = clarification.originRequestSha256;
  QJsonArray options;
  for(const ClarificationOption &option : clarification.options)
  {
    QJsonObject row;
    row[lit("id")] = option.id;    // i18n-ignore: clarification protocol key
    row[lit("label")] = option.label;    // i18n-ignore: clarification protocol key
    if(!option.description.isEmpty())
      row[lit("description")] = option.description;    // i18n-ignore: clarification protocol key
    options.push_back(row);
  }
  object[lit("options")] = options;    // i18n-ignore: clarification protocol key
  const QJsonObject origin = contextCandidateJSON(clarification.originContextSnapshot,
                                                  clarification.originContextInstanceId);
  if(!origin.isEmpty())
  {
    // i18n-ignore: Persisted ReplayLab clarification protocol field names.
    object[lit("originContextSnapshot")] = origin.value(lit("snapshot"));
    object[lit("originContextInstanceId")] =    // i18n-ignore: persisted clarification protocol key
        clarification.originContextInstanceId;
  }
  return object;
}

void AIAssistant::bindClarificationOrigin(ClarificationPrompt &clarification,
                                          const QString &taskId) const
{
  const ChatExecution *execution = chatExecution(taskId);
  if(execution)
  {
    // Bind only the trusted native submit-time request. Durable workflows retain a long request
    // in AgentHost; storing a prefix here would falsely present it as exact on resume.
    const QString &originRequest = execution->request.effectivePrompt;
    clarification.originRequestSha256 = clarificationOriginSha256(originRequest);
    clarification.originRequest = originRequest.size() <= MaxClarificationOriginChars
                                      ? originRequest
                                      : QString();
    if((!decodeContextSnapshot(clarification.originContextSnapshot) ||
        clarification.originContextInstanceId.trimmed().isEmpty()) &&
       decodeContextSnapshot(execution->request.contextSnapshot) &&
       !execution->request.contextInstanceId.trimmed().isEmpty())
    {
      clarification.originContextSnapshot = execution->request.contextSnapshot;
      clarification.originContextInstanceId = execution->request.contextInstanceId;
    }
  }
}

void AIAssistant::updateClarificationUI()
{
  if(!m_ClarificationPanel || !m_ClarificationOptionsLayout)
    return;

  const ConversationThread *thread = conversation();
  const ClarificationPrompt *clarification =
      thread && thread->clarification.isValid() ? &thread->clarification : NULL;
  const QString key = clarification ? thread->id + lit("\n") + clarification->id : QString();
  if(clarification && key == m_VisibleClarificationKey)
  {
    m_ClarificationPanel->setVisible(true);
    updateClarificationSubmitState();
    return;
  }

  delete m_ClarificationButtonGroup;
  m_ClarificationButtonGroup = NULL;
  m_ClarificationButtons.clear();
  m_ClarificationOtherEdit = NULL;
  while(QLayoutItem *item = m_ClarificationOptionsLayout->takeAt(0))
  {
    delete item->widget();
    delete item;
  }
  m_VisibleClarificationKey.clear();

  if(!clarification)
  {
    m_ClarificationPanel->setVisible(false);
    return;
  }

  m_VisibleClarificationKey = key;
  m_ClarificationQuestion->setText(clarification->question);
  m_ClarificationButtonGroup = new QButtonGroup(m_ClarificationPanel);
  // i18n-ignore: ReplayLab clarification selection-mode enum value.
  m_ClarificationButtonGroup->setExclusive(clarification->selectionMode == lit("single"));
  const QString choiceStyle =
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QCommandLinkButton { border: 1px solid palette(mid); border-radius: 5px; "
          "padding: 5px; text-align: left; } "
          "QCommandLinkButton:hover { background: palette(midlight); } "
          "QCommandLinkButton:checked { border: 2px solid palette(highlight); "
          "background: palette(base); }");
  for(const ClarificationOption &option : clarification->options)
  {
    QCommandLinkButton *choice = new QCommandLinkButton(option.label, m_ClarificationOptions);
    choice->setDescription(option.description);
    choice->setAccessibleName(option.label);
    choice->setAccessibleDescription(option.description);
    choice->setCheckable(true);
    choice->setStyleSheet(choiceStyle);
    // i18n-ignore: Dynamic QObject properties are internal clarification protocol state.
    choice->setProperty("clarificationOptionId", option.id);
    choice->setProperty("clarificationOptionLabel", option.label);
    choice->setProperty("clarificationOther", false);
    m_ClarificationButtonGroup->addButton(choice);
    m_ClarificationButtons.push_back(choice);
    m_ClarificationOptionsLayout->addWidget(choice);
    connect(choice, &QCommandLinkButton::toggled, this,
            [this](bool) { updateClarificationSubmitState(); });
  }

  if(clarification->allowOther)
  {
    QCommandLinkButton *other = new QCommandLinkButton(tr("Other"), m_ClarificationOptions);
    other->setDescription(tr("Enter a different answer."));
    other->setAccessibleName(tr("Other"));
    other->setAccessibleDescription(tr("Enter a different answer."));
    other->setCheckable(true);
    other->setStyleSheet(choiceStyle);
    other->setProperty("clarificationOther", true);
    m_ClarificationButtonGroup->addButton(other);
    m_ClarificationButtons.push_back(other);
    m_ClarificationOptionsLayout->addWidget(other);
    m_ClarificationOtherEdit = new QLineEdit(m_ClarificationOptions);
    m_ClarificationOtherEdit->setPlaceholderText(tr("Enter another answer"));
    m_ClarificationOtherEdit->setMaxLength(MaxClarificationOtherChars);
    m_ClarificationOtherEdit->setEnabled(false);
    m_ClarificationOptionsLayout->addWidget(m_ClarificationOtherEdit);
    connect(other, &QCommandLinkButton::toggled, this, [this](bool checked) {
      if(m_ClarificationOtherEdit)
      {
        m_ClarificationOtherEdit->setEnabled(checked);
        if(checked)
          m_ClarificationOtherEdit->setFocus(Qt::OtherFocusReason);
      }
      updateClarificationSubmitState();
    });
    connect(m_ClarificationOtherEdit, &QLineEdit::textChanged, this,
            [this, other](const QString &text) {
              if(!text.trimmed().isEmpty() && !other->isChecked())
                other->setChecked(true);
              updateClarificationSubmitState();
            });
  }

  QString submitText = tr("Continue");
  // submitLabel is a provider hint, never unrestricted UI text. Localise only the stable values
  // known by qrenderdoc and fall back to the canonical Continue label for unknown values.
  if(clarification->submitLabel.compare(lit("Confirm"), Qt::CaseInsensitive) == 0)    // i18n-ignore: submitLabel protocol enum
    submitText = tr("Confirm");
  else if(clarification->submitLabel.compare(lit("Submit"), Qt::CaseInsensitive) == 0)    // i18n-ignore: submitLabel protocol enum
    submitText = tr("Submit");
  m_ClarificationSubmit->setText(submitText);
  m_ClarificationPanel->setVisible(true);
  updateClarificationSubmitState();
}

void AIAssistant::updateClarificationSubmitState()
{
  if(!m_ClarificationSubmit)
    return;
  bool anySelected = false;
  bool selectedOtherIsEmpty = false;
  for(QCommandLinkButton *choice : m_ClarificationButtons)
  {
    if(!choice || !choice->isChecked())
      continue;
    anySelected = true;
    if(choice->property("clarificationOther").toBool() &&
       (!m_ClarificationOtherEdit || m_ClarificationOtherEdit->text().trimmed().isEmpty()))
      selectedOtherIsEmpty = true;
  }
  m_ClarificationSubmit->setEnabled(anySelected && !selectedOtherIsEmpty);
}

void AIAssistant::submitClarification()
{
  const ConversationThread *thread = conversation();
  if(!thread || !thread->clarification.isValid())
    return;

  QStringList selected;
  QStringList selectedIds;
  QString otherText;
  for(QCommandLinkButton *choice : m_ClarificationButtons)
  {
    if(!choice || !choice->isChecked())
      continue;
    if(choice->property("clarificationOther").toBool())
    {
      if(m_ClarificationOtherEdit)
        otherText = m_ClarificationOtherEdit->text().trimmed();
    }
    else
    {
      const QString optionId = choice->property("clarificationOptionId").toString();    // i18n-ignore: internal clarification property
      const QString label = choice->property("clarificationOptionLabel").toString().simplified();
      if(!optionId.isEmpty() && !label.isEmpty())
      {
        selectedIds.push_back(optionId);
        selected.push_back(label);
      }
    }
  }

  QString answer;
  // i18n-ignore: ReplayLab clarification selection-mode enum value.
  if(thread->clarification.selectionMode == lit("single"))
  {
    if(!otherText.isEmpty())
      answer = otherText;
    else if(selected.size() == 1)
      answer = tr("My choice: %1").arg(selected.front());
  }
  else
  {
    if(!otherText.isEmpty())
      selected.push_back(otherText);
    if(!selected.isEmpty())
    {
      QStringList rows;
      for(const QString &item : selected)
        rows.push_back(lit("- ") + item);
      answer = tr("My choices:\n%1").arg(rows.join(QLatin1Char('\n')));
    }
  }
  if(!answer.trimmed().isEmpty())
  {
    ClarificationResponse response;
    response.clarificationId = thread->clarification.id;
    response.optionIds = selectedIds;
    response.otherText = otherText;
    response.originRequest = thread->clarification.originRequest;
    response.originRequestSha256 = thread->clarification.originRequestSha256;
    response.originContextSnapshot = thread->clarification.originContextSnapshot;
    response.originContextInstanceId = thread->clarification.originContextInstanceId;
    // Keep the readable answer in the transcript. Stable IDs and origin/current context candidates
    // travel separately in replaylab-conversation/v2 for intent-aware resolution by AgentHost.
    startChat(answer, true, QString(), response);
  }
}

void AIAssistant::clearClarification(const QString &conversationId)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread || !thread->clarification.isValid())
    return;
  thread->clarification = ClarificationPrompt();
  if(thread->id == m_ActiveConversationId)
  {
    m_VisibleClarificationKey.clear();
    updateClarificationUI();
  }
  scheduleConversationSave();
}

void AIAssistant::updateTaskProgressUI()
{
  if(!m_TaskProgress || !m_TaskProgressMenu)
    return;

  // Only the selected conversation's live execution can be the main task. This prevents a plan
  // from another conversation (or a completed historical turn) leaking into the composer.
  const ConversationThread *thread = conversation();
  QString taskId;
  if(thread)
  {
    const ChatExecution *execution = conversationExecution(thread->id);
    if(execution)
      taskId = execution->request.taskId;
    if(taskId.isEmpty())
    {
      for(int i = m_ChatQueue.size() - 1; i >= 0; --i)
      {
        if(m_ChatQueue[i].conversationId == thread->id)
        {
          taskId = m_ChatQueue[i].taskId;
          break;
        }
      }
    }
  }

  const AIChatMessage *mainTask = taskId.isEmpty() ? NULL : taskMessage(taskId);
  if(mainTask == NULL || !m_RunningTasks.contains(taskId))
  {
    m_TaskProgressMenu->hide();
    m_TaskProgressMenu->clear();
    m_TaskProgress->hide();
    return;
  }

  struct TaskEntry
  {
    QString id;
    QString title;
    QString status;
    qint64 startedMs = 0;
    qint64 finishedMs = 0;
    qint64 lastTimestampMs = 0;
    bool hasLifecycle = false;
  };
  QVector<TaskEntry> entries;
  QHash<QString, int> entryIndexes;

  const auto statusForActivity = [](const AIActivityItem &activity) {
    // Approval and input requests are waiting states, even though their activity rows carry the
    // attention/error styling used by the transcript. No provider output or tool payload enters
    // this compact projection.
    const bool waiting =
        activity.kind == lit("approval") ||    // i18n-ignore: task activity protocol ID
        activity.kind == lit("needs-user-input") ||    // i18n-ignore: task activity protocol ID
        activity.kind == lit("queued");    // i18n-ignore: task activity protocol ID
    if(!activity.completed)
    {
      // i18n-ignore: Internal projected task-status IDs; visible labels are translated below.
      return waiting ? lit("waiting") : lit("running");
    }
    if(waiting)
      return lit("running");    // i18n-ignore: internal projected task-status ID
    // i18n-ignore: Task activity protocol IDs used only to project a translated status.
    if(activity.error || activity.kind == lit("failed") || activity.kind == lit("cancelled") ||
       // i18n-ignore: Task activity protocol IDs used only to project a translated status.
       activity.kind == lit("interrupted") || activity.kind == lit("blocked"))
      return lit("failed");    // i18n-ignore: internal projected task-status ID
    return lit("completed");    // i18n-ignore: internal projected task-status ID
  };

  for(const AIActivityItem &activity : mainTask->activity)
  {
    QString planTaskId = activity.id;
    if(planTaskId.isEmpty() || !safeTaskId(planTaskId))
      continue;

    // A task ID is reused by child tools and messages. Only the exact child lifecycle kind may
    // create or change the status of the whole step.
    const bool childLifecycle =
        // i18n-ignore: Stable task activity protocol prefix.
        activity.kind == lit("child-") + planTaskId;
    const bool waitingLifecycle =
        activity.kind == lit("approval") ||    // i18n-ignore: task activity protocol ID
        activity.kind == lit("needs-user-input") ||    // i18n-ignore: task activity protocol ID
        activity.kind == lit("queued");    // i18n-ignore: task activity protocol ID
    if(!childLifecycle && !(waitingLifecycle && entryIndexes.contains(planTaskId)))
      continue;

    int index = entryIndexes.value(planTaskId, -1);
    if(index < 0)
    {
      index = entries.size();
      entryIndexes.insert(planTaskId, index);
      entries.push_back(TaskEntry());
      entries[index].id = planTaskId;
    }

    TaskEntry &entry = entries[index];
    if(entry.title.isEmpty() && !activity.title.trimmed().isEmpty())
      entry.title = activity.title.trimmed();
    const qint64 timestamp = activity.timestampMs > 0 ? activity.timestampMs : 0;
    if(entry.startedMs <= 0 || (timestamp > 0 && timestamp < entry.startedMs))
      entry.startedMs = timestamp;
    if(activity.finishedTimestampMs > 0)
      entry.finishedMs = qMax(entry.finishedMs, activity.finishedTimestampMs);
    const qint64 lifecycleTimestamp = qMax(timestamp, activity.finishedTimestampMs);
    if(!entry.hasLifecycle || lifecycleTimestamp >= entry.lastTimestampMs)
    {
      entry.status = statusForActivity(activity);
      entry.lastTimestampMs = lifecycleTimestamp;
      entry.hasLifecycle = true;
      // A finished review or approval resumes the parent step; it must not freeze that step's
      // elapsed clock at the subactivity's completion time.
      if(entry.status != lit("completed") &&    // i18n-ignore: internal projected task-status ID
         entry.status != lit("failed"))    // i18n-ignore: internal projected task-status ID
        entry.finishedMs = 0;
    }
  }

  if(entries.size() < 2)
  {
    m_TaskProgressMenu->hide();
    m_TaskProgressMenu->clear();
    m_TaskProgress->hide();
    return;
  }

  int currentStep = entries.size();
  for(int i = 0; i < entries.size(); ++i)
  {
    // i18n-ignore: Internal projected task-status IDs; visible labels are translated below.
    if(entries[i].status == lit("running") || entries[i].status == lit("waiting"))
    {
      currentStep = i + 1;
      break;
    }
  }

  const QString stepText = tr("Step %1 of %2").arg(currentStep).arg(entries.size());
  const QString stepToolTip =
      tr("Task progress: step %1 of %2").arg(currentStep).arg(entries.size());
  const QString stepAccessibleDescription =
      tr("Active AI task: step %1 of %2. Open to inspect task statuses.")
          .arg(currentStep)
          .arg(entries.size());
  if(m_TaskProgress->text() != stepText)
    m_TaskProgress->setText(stepText);
  if(m_TaskProgress->toolTip() != stepToolTip)
    m_TaskProgress->setToolTip(stepToolTip);
  if(m_TaskProgress->accessibleDescription() != stepAccessibleDescription)
    m_TaskProgress->setAccessibleDescription(stepAccessibleDescription);

  const qint64 now = QDateTime::currentMSecsSinceEpoch();
  const int progressParentWidth =
      m_TaskProgress->parentWidget() ? m_TaskProgress->parentWidget()->width() : 640;
  const int progressRowWidth = qBound(320, progressParentWidth - 32, 680);

  struct ProgressRow
  {
    QString id;
    QString text;
    AITaskProgressRow::State state = AITaskProgressRow::State::Running;
  };
  QVector<ProgressRow> progressRows;
  progressRows.reserve(entries.size());
  for(const TaskEntry &entry : entries)
  {
    QString status;
    AITaskProgressRow::State rowState = AITaskProgressRow::State::Running;
    if(entry.status == lit("completed"))    // i18n-ignore: internal projected task-status ID
    {
      status = tr("step ended");
      rowState = AITaskProgressRow::State::Completed;
    }
    else if(entry.status == lit("failed"))    // i18n-ignore: internal projected task-status ID
    {
      status = tr("failed");
      rowState = AITaskProgressRow::State::Failed;
    }
    else if(entry.status == lit("waiting"))    // i18n-ignore: internal projected task-status ID
    {
      status = tr("waiting");
      rowState = AITaskProgressRow::State::Waiting;
    }
    else
      status = tr("running");

    QString elapsed;
    if(entry.startedMs > 0)
    {
      const qint64 end = entry.finishedMs > 0 ? entry.finishedMs : now;
      const qint64 elapsedMs = qMax<qint64>(0, end - entry.startedMs);
      elapsed = tr("%1 s").arg(QString::number(double(elapsedMs) / 1000.0, 'f', 1));
    }
    QString title = entry.title.simplified();
    if(title.isEmpty())
      title = tr("Untitled step");
    if(title.size() > 160)
      title = title.left(157) + lit("...");
    const QString rowText = elapsed.isEmpty()
                                ? tr("%1 - %2").arg(title, status)
                                : tr("%1 - %2 | %3").arg(title, status, elapsed);
    progressRows.push_back({entry.id, rowText, rowState});
  }

  // Streaming transcript updates render at roughly 16 fps. Rebuilding an open native QMenu on
  // every render destroys and recreates its child windows, which visibly flashes and can disturb
  // popup activation. Reuse the bounded rows while the task identity/order is unchanged.
  const QList<QAction *> actions = m_TaskProgressMenu->actions();
  bool reuseRows = actions.size() == progressRows.size() + 2 && actions.size() >= 2 &&
                   actions[1]->isSeparator();
  if(reuseRows)
  {
    for(int i = 0; i < progressRows.size(); ++i)
    {
      QWidgetAction *rowAction = qobject_cast<QWidgetAction *>(actions[i + 2]);
      AITaskProgressRow *row =
          rowAction ? dynamic_cast<AITaskProgressRow *>(rowAction->defaultWidget()) : NULL;
      if(!rowAction || !row || rowAction->data().toString() != progressRows[i].id)
      {
        reuseRows = false;
        break;
      }
    }
  }

  if(reuseRows)
  {
    for(int i = 0; i < progressRows.size(); ++i)
    {
      QWidgetAction *rowAction = static_cast<QWidgetAction *>(actions[i + 2]);
      AITaskProgressRow *row = static_cast<AITaskProgressRow *>(rowAction->defaultWidget());
      const ProgressRow &progressRow = progressRows[i];
      if(rowAction->text() != progressRow.text)
      {
        rowAction->setText(progressRow.text);
        rowAction->setToolTip(progressRow.text);
        rowAction->setStatusTip(progressRow.text);
      }
      row->updateProgress(progressRow.text, progressRow.state, progressRowWidth);
    }
  }
  else
  {
    m_TaskProgressMenu->setUpdatesEnabled(false);
    m_TaskProgressMenu->clear();
    QAction *heading = m_TaskProgressMenu->addAction(tr("Task progress"));
    heading->setEnabled(false);
    m_TaskProgressMenu->addSeparator();
    for(const ProgressRow &progressRow : progressRows)
    {
      QWidgetAction *rowAction = new QWidgetAction(m_TaskProgressMenu);
      rowAction->setData(progressRow.id);
      rowAction->setText(progressRow.text);
      rowAction->setToolTip(progressRow.text);
      rowAction->setStatusTip(progressRow.text);
      rowAction->setDefaultWidget(new AITaskProgressRow(
          progressRow.text, progressRow.state, progressRowWidth, m_TaskProgressMenu));
      m_TaskProgressMenu->addAction(rowAction);
    }
    m_TaskProgressMenu->setUpdatesEnabled(true);
    m_TaskProgressMenu->update();
  }
  if(!m_TaskProgress->isVisible())
    m_TaskProgress->show();
}

void AIAssistant::updateComposerState()
{
  if(!m_QueueStatus)
    return;
  QString status;
  ChatExecution *activeExecution = conversationExecution(m_ActiveConversationId);
  const bool activeRunning =
      activeExecution && !m_CompletedTasks.contains(activeExecution->request.taskId);
  if(activeRunning)
    status = tr("Running");
  int otherConversationCount = 0;
  for(auto it = m_ChatExecutions.cbegin(); it != m_ChatExecutions.cend(); ++it)
    if(it.value() != activeExecution && !m_CompletedTasks.contains(it.key()))
      ++otherConversationCount;
  if(otherConversationCount > 0)
    status += (status.isEmpty() ? QString() : lit(" | ")) +
              tr("%1 other conversation(s) running").arg(otherConversationCount);
  int captureTaskCount = m_RunningTasks.size();
  for(auto it = m_ChatExecutions.cbegin(); it != m_ChatExecutions.cend(); ++it)
    captureTaskCount -= m_RunningTasks.contains(it.key()) ? 1 : 0;
  captureTaskCount = qMax(0, captureTaskCount);
  if(captureTaskCount > 0)
    status += (status.isEmpty() ? QString() : lit(" | ")) +
              tr("%1 capture action(s) running").arg(captureTaskCount);
  if(!m_ChatQueue.isEmpty())
    status += (status.isEmpty() ? QString() : lit(" | ")) +
              tr("%1 queued").arg(m_ChatQueue.size());
  m_QueueStatus->setText(status);
  refreshPendingChats();
  if(m_Cancel)
  {
    const bool canStopChat = activeRunning && activeExecution->process &&
                             activeExecution->process->state() != QProcess::NotRunning;
    m_Cancel->setVisible(canStopChat);
    m_Cancel->setEnabled(canStopChat);
    m_Cancel->setToolTip(tr("Stop the active chat request."));
  }
  if(m_Send)
    m_Send->setEnabled(true);
  updateClarificationUI();
  updateTaskProgressUI();
}

void AIAssistant::handleTranscriptLink(const QUrl &url)
{
  const QString scheme = url.scheme().toLower();
  if(scheme == QLatin1String("replaylab-annotation") && url.host() == QLatin1String("edit"))
  {
    editRenderTargetAnnotation(QUrl::fromPercentEncoding(url.path().mid(1).toUtf8()));
    return;
  }
  if(scheme == lit("http") || scheme == lit("https"))
  {
    QDesktopServices::openUrl(url);
    return;
  }
  // i18n-ignore: Internal link scheme for Host-verified task artifacts.
  if(scheme == lit("replaylab-artifact"))
  {
    const QString path = QUrl::fromPercentEncoding(url.path().mid(1).toUtf8());
    const QFileInfo info(path);
    if(path.isEmpty() || !info.exists())
    {
      appendSystem(tr("The artifact no longer exists: %1").arg(path), true);
      return;
    }
    // i18n-ignore: Internal link host is a fixed action identifier.
    if(url.host().compare(lit("reveal"), Qt::CaseInsensitive) == 0)
    {
#if defined(Q_OS_WIN)
      QStringList arguments;
      if(!info.isDir())
        arguments << lit("/select,");    // i18n-ignore: Windows Explorer command-line switch.
      arguments << QDir::toNativeSeparators(info.absoluteFilePath());
      if(QProcess::startDetached(lit("explorer.exe"), arguments))    // i18n-ignore: Windows shell executable name.
        return;
#endif
      QDesktopServices::openUrl(QUrl::fromLocalFile(info.isDir() ? info.absoluteFilePath()
                                                                  : info.absolutePath()));
      return;
    }
    const QString suffix = info.suffix().toLower();
    if(suffix == QLatin1String("md") || suffix == QLatin1String("markdown") ||
       suffix == QLatin1String("html") || suffix == QLatin1String("htm") || suffix == QLatin1String("txt"))
    {
      QJsonObject request;
      request[QStringLiteral("path")] = info.absoluteFilePath();
      QString error;
      m_Ctx.ReplayLab().OpenDocumentJSON(
          QString::fromUtf8(QJsonDocument(request).toJson(QJsonDocument::Compact)), &error);
      if(error.isEmpty())
        return;
      appendSystem(error, true);
      return;
    }
    QDesktopServices::openUrl(QUrl::fromLocalFile(info.absoluteFilePath()));
    return;
  }
  if(scheme != lit("rdoc") || !m_Ctx.IsCaptureLoaded())
  {
    appendSystem(tr("The link is not an allowed RenderDoc navigation target."), true);
    return;
  }

  const QString target = url.host().toLower();
  QString value = QUrl::fromPercentEncoding(url.path().mid(1).toUtf8());
  if(target == QLatin1String("cbuffer") || target == QLatin1String("uv"))
  {
    const QJsonObject saved = AIVisualReply::navigationTarget(value);
    const QJsonObject capture = saved.value(QLatin1String("capture")).toObject();
    const QJsonObject leaf = saved.value(QLatin1String("target")).toObject();
    const QFileInfo current(QString::fromUtf8(m_Ctx.GetCaptureFilename().c_str()));
    const QFileInfo original(capture.value(QLatin1String("path")).toString());
    QString activeSession;
    const bool sameSession =
        findBridgeBinding(NULL, &activeSession) && !activeSession.isEmpty() &&
        activeSession == capture.value(QLatin1String("captureSessionId")).toString();
    const bool sameCapture =
        current.exists() && original.exists() &&
        current.canonicalFilePath().compare(original.canonicalFilePath(), Qt::CaseInsensitive) == 0 &&
        (sameSession || (current.size() == qint64(capture.value(QLatin1String("size")).toDouble(-1)) &&
                         current.lastModified().toMSecsSinceEpoch() ==
                             qint64(capture.value(QLatin1String("mtimeMs")).toDouble(-1))));
    const uint32_t eid = uint32_t(leaf.value(QLatin1String("eventId")).toDouble());
    if(target == QLatin1String("uv"))
    {
      if(sameCapture && eid && leaf.value(QLatin1String("kind")).toString() == QLatin1String("uv"))
      {
        const QString stageName = leaf.value(QLatin1String("stage")).toString();
        const int stageIndex = stageName == QLatin1String("vs-out")   ? 1
                               : stageName == QLatin1String("gs-out") ? 2
                                                                      : 0;
        const qulonglong raw =
            leaf.value(QLatin1String("textureResourceId")).toString().toULongLong();
        ResourceId texture;
        memcpy(&texture, &raw, sizeof(texture));
        if(m_Ctx.CurEvent() != eid || m_Ctx.CurSelectedEvent() != eid)
          m_Ctx.SetEventID({}, eid, eid, true);
        if(BufferViewer *viewer = qobject_cast<BufferViewer *>(m_Ctx.GetMeshPreview()->Widget()))
        {
          m_Ctx.AddDockWindow(viewer, DockReference::MainToolArea, this);
          if(leaf.contains(QLatin1String("framing")) ||
             leaf.contains(QLatin1String("selectionKeyData")))
            viewer->setMeshEvidence(leaf.toVariantMap());
          else
          {
            viewer->NavigateMeshUV(stageIndex, leaf.value(QLatin1String("channel")).toString(),
                                   leaf.value(QLatin1String("pair")).toInt() == 1 ? 2 : 0, texture);
            if(leaf.contains(QLatin1String("selectedVertices")))
              viewer->NavigateMeshSelection(
                  leaf.value(QLatin1String("selectionMode")).toString(),
                  leaf.value(QLatin1String("selectedVertices")).toArray().toVariantList(),
                  leaf.value(QLatin1String("selectedElements")).toArray().toVariantList());
          }
          m_Ctx.RaiseDockWindow(viewer);
          viewer->show();
        }
      }
      else
        appendSystem(tr("This UV link no longer matches the source capture."), true);
      return;
    }
    const QMap<QString, ShaderStage> stages = {{QLatin1String("vertex"), ShaderStage::Vertex},
                                               {QLatin1String("fragment"), ShaderStage::Pixel},
                                               {QLatin1String("compute"), ShaderStage::Compute},
                                               {QLatin1String("geometry"), ShaderStage::Geometry},
                                               {QLatin1String("hull"), ShaderStage::Hull},
                                               {QLatin1String("domain"), ShaderStage::Domain},
                                               {QLatin1String("task"), ShaderStage::Task},
                                               {QLatin1String("mesh"), ShaderStage::Mesh}};
    const ShaderStage stage =
        stages.value(leaf.value(QLatin1String("stage")).toString(), ShaderStage::Count);
    const int block = leaf.value(QLatin1String("blockIndex")).toInt(-1);
    const int index = leaf.value(QLatin1String("arrayIndex")).toInt(-1);
    if(sameCapture && stage != ShaderStage::Count && eid && block >= 0 && index >= 0)
    {
      // A private evidence query may have restored pixels without republishing the GUI pipeline.
      m_Ctx.SetEventID({}, eid, eid, true);
      const PipeState &pipe = m_Ctx.CurPipelineState();
      const ShaderReflection *reflection = pipe.GetShaderReflection(stage);
      const UsedDescriptor binding = pipe.GetConstantBlock(stage, uint32_t(block), uint32_t(index));
      const QString expectedHash = leaf.value(QLatin1String("shaderRawSha256")).toString();
      auto hashReflection = [](const ShaderReflection *shader) {
        if(!shader || shader->rawBytes.empty())
          return QString();
        return QString::fromLatin1(
            QCryptographicHash::hash(
                QByteArray::fromRawData(reinterpret_cast<const char *>(shader->rawBytes.data()),
                                        int(shader->rawBytes.size())),
                QCryptographicHash::Sha256)
                .toHex());
      };
      QString shaderHash = hashReflection(reflection);
      if(shaderHash != expectedHash)
      {
        // Use the same exact pipeline/entry lookup as the Host's reflected target.
        const ResourceId shader = pipe.GetShader(stage);
        const ResourceId pipeline = stage == ShaderStage::Compute ? pipe.GetComputePipelineObject()
                                                                  : pipe.GetGraphicsPipelineObject();
        ShaderEntryPoint entry;
        entry.name = pipe.GetShaderEntryPoint(stage);
        entry.stage = stage;
        m_Ctx.Replay().BlockInvoke([&](IReplayController *controller) {
          shaderHash = hashReflection(controller->GetShader(pipeline, shader, entry));
        });
      }
      uint64_t resource = 0;
      memcpy(&resource, &binding.descriptor.resource, sizeof(resource));
      if(reflection && uint32_t(block) < reflection->constantBlocks.size() &&
         shaderHash == expectedHash &&
         QString::number(resource) == leaf.value(QLatin1String("bufferResourceId")).toString())
      {
        IBufferViewer *viewer = m_Ctx.ViewConstantBuffer(stage, uint32_t(block), uint32_t(index));
        if(BufferViewer *native = qobject_cast<BufferViewer *>(viewer->Widget()))
        {
          // New CBuffer viewers defer their reads until they are actually docked/shown,
          // just as when opening one from the pipeline state. Raising an undocked
          // widget leaves its reflected rows permanently pending.
          m_Ctx.AddDockWindow(native, DockReference::TransientPopupArea, this, 0.3f);
          native->NavigateCBufferVariable(
              leaf.value(QLatin1String("path")).toString(),
              qint64(leaf.value(QLatin1String("byteOffset")).toDouble(-1)),
              leaf.value(QLatin1String("component")).toInt(-1));
          m_Ctx.RaiseDockWindow(native);
          return;
        }
      }
    }
    appendSystem(
        tr("This CBuffer link no longer matches the source capture or reflected parameter."), true);
    return;
  }
  if(target == QLatin1String("shader"))
  {
    const QUrlQuery query(url);
    const QString expectedHash = query.queryItemValue(QStringLiteral("sourceSha256"));
    if(!expectedHash.isEmpty())
    {
      QFile source(query.queryItemValue(QStringLiteral("path"), QUrl::FullyDecoded));
      if(!AIVisualReply::validIdentity(expectedHash) || source.size() > 32 * 1024 * 1024 ||
         !source.open(QIODevice::ReadOnly) ||
         QString::fromLatin1(
             QCryptographicHash::hash(source.readAll(), QCryptographicHash::Sha256).toHex()) !=
             expectedHash)
      {
        appendSystem(
            tr("The saved Shader source changed. Refresh this code excerpt before navigating."),
            true);
        return;
      }
    }
    QJsonObject request;
    request[QStringLiteral("workspaceId")] = value;
    request[QStringLiteral("path")] = query.queryItemValue(QStringLiteral("path"), QUrl::FullyDecoded);
    request[QStringLiteral("file")] = query.queryItemValue(QStringLiteral("file"), QUrl::FullyDecoded);
    bool lineOK = true, columnOK = true;
    const int line = query.hasQueryItem(QStringLiteral("line"))
                         ? query.queryItemValue(QStringLiteral("line")).toInt(&lineOK) : 1;
    const int column = query.hasQueryItem(QStringLiteral("column"))
                           ? query.queryItemValue(QStringLiteral("column")).toInt(&columnOK) : 1;
    if(lineOK && columnOK && line > 0 && column > 0 && line <= 10000000 && column <= 1000000 &&
       value.size() <= 256 && url.toString().size() <= 8192 &&
       url.userInfo().isEmpty() && url.port() == -1)
    {
      request[QStringLiteral("line")] = line;
      request[QStringLiteral("column")] = column;
      request[QStringLiteral("symbol")] = query.queryItemValue(QStringLiteral("symbol"));
      request[QStringLiteral("selectLine")] =
          query.queryItemValue(QStringLiteral("selectLine")) == QStringLiteral("1");
      request[QStringLiteral("sourceSha256")] =
          query.queryItemValue(QStringLiteral("sourceSha256"));
      QString error;
      const QString result = m_Ctx.ReplayLab().NavigateShaderSourceJSON(
          QString::fromUtf8(QJsonDocument(request).toJson(QJsonDocument::Compact)), &error);
      if(error.isEmpty() && QJsonDocument::fromJson(result.toUtf8()).object()
                                .value(QStringLiteral("accepted")).toBool())
        return;
    }
    appendSystem(tr("The Shader source link is unavailable in the current workspace."), true);
    return;
  }
  if(target == QLatin1String("event") || target == QLatin1String("eid") ||
     target == QLatin1String("mesh"))
  {
    bool ok = false;
    const uint32_t eid = value.toUInt(&ok);
    if(ok && eid > 0)
    {
      m_Ctx.SetEventID({}, eid, eid);
      if(target == QLatin1String("mesh"))
        m_Ctx.ShowMeshPreview();
      return;
    }
  }
  else if(target == lit("resource") || target == lit("texture") || target == lit("buffer"))
  {
    value.remove(lit("ResourceId::"), Qt::CaseInsensitive);
    bool ok = false;
    const qulonglong raw = value.toULongLong(&ok);
    if(ok && raw != 0)
    {
      ResourceId id;
      static_assert(sizeof(id) == sizeof(raw), "ResourceId wire size changed");
      memcpy(&id, &raw, sizeof(id));
      const QUrlQuery query(url);
      const bool eventScoped = query.hasQueryItem(QLatin1String("eid"));
      if(target == QLatin1String("texture") ||
         (target == QLatin1String("resource") && eventScoped && m_Ctx.GetTexture(id)))
      {
        bool validEvent = true;
        const uint32_t eid =
            eventScoped ? query.queryItemValue(QLatin1String("eid")).toUInt(&validEvent) : 0;
        if(!m_Ctx.GetTexture(id) || (eventScoped && (!validEvent || !eid || !m_Ctx.GetAction(eid))))
        {
          appendSystem(tr("The link is not an allowed RenderDoc navigation target."), true);
          return;
        }
        if(eid && (m_Ctx.CurEvent() != eid || m_Ctx.CurSelectedEvent() != eid))
          m_Ctx.SetEventID({}, eid, eid, true);
        m_Ctx.ShowTextureViewer();
        TextureViewer *viewer = qobject_cast<TextureViewer *>(m_Ctx.GetTextureViewer()->Widget());
        if(!viewer || (eid && m_Ctx.CurEvent() != eid) ||
           !viewer->FollowBoundTexture(id, query.queryItemValue(QLatin1String("kind"))))
          appendSystem(tr("This texture is not bound to an input or output at the linked event."),
                       true);
        return;
      }
      if(target == lit("buffer"))
      {
        const BufferDescription *buffer = m_Ctx.GetBuffer(id);
        if(buffer)
        {
          m_Ctx.ViewBuffer(0, buffer->length, id, QString());
          return;
        }
      }
      IResourceInspector *inspector = m_Ctx.GetResourceInspector();
      inspector->Inspect(id);
      m_Ctx.RaiseDockWindow(inspector->Widget());
      return;
    }
  }
  else if(target == lit("panel"))
  {
    const QString panel = value.toLower();
    if(panel == lit("pipeline"))
      m_Ctx.ShowPipelineViewer();
    else if(panel == lit("texture"))
      m_Ctx.ShowTextureViewer();
    else if(panel == lit("resources"))
      m_Ctx.RaiseDockWindow(m_Ctx.GetResourceInspector()->Widget());
    else
      appendSystem(tr("Unknown RenderDoc panel link: %1").arg(value), true);
    return;
  }
  appendSystem(tr("The RenderDoc link target is invalid."), true);
}

QJsonObject AIAssistant::conversationToJSON(const ConversationThread &thread,
                                            int *remainingChars) const
{
  QJsonObject object;
  object[lit("id")] = thread.id;
  object[lit("title")] = thread.title.left(256);
  // i18n-ignore: Stable AI connection/model identifiers in the local conversation store.
  object[lit("profileId")] = thread.profileId;
  // i18n-ignore: Stable AI connection/model identifiers in the local conversation store.
  object[lit("modelId")] = thread.modelId;
  object[lit("createdUnixMs")] = QString::number(thread.createdMs);
  object[lit("updatedUnixMs")] = QString::number(thread.updatedMs);
  object[lit("pinned")] = thread.pinned;
  object[lit("unread")] = thread.unread;
  if(thread.clarification.isValid())
  {
    const QJsonObject clarificationObject = clarificationToJSON(thread.clarification);
    // UTF-8 bytes conservatively count against the UTF-16 character budget. This keeps a
    // persisted origin snapshot from evicting newer transcript rows beyond the store bound.
    const int clarificationChars =
        QJsonDocument(clarificationObject).toJson(QJsonDocument::Compact).size();
    if(clarificationChars <= *remainingChars)
    {
      // i18n-ignore: Persisted ReplayLab clarification protocol field name.
      object[lit("clarification")] = clarificationObject;
      *remainingChars -= clarificationChars;
    }
  }
  QJsonArray messages;
  QVector<QJsonObject> selectedMessages;
  int eligibleMessageCount = 0;
  for(const AIChatMessage &item : thread.messages)
    if(item.role == lit("user") || item.role == lit("assistant"))
      eligibleMessageCount++;
  const int first = qMax(0, thread.messages.size() - MaxConversationMessages);
  // Allocate the bounded store from newest to oldest. Prepending the selected rows restores
  // chronological display order without allowing old history to evict the latest turn.
  for(int i = thread.messages.size() - 1; i >= first && *remainingChars > 0; --i)
  {
    const AIChatMessage &item = thread.messages[i];
    if(item.role != lit("user") && item.role != lit("assistant"))
      continue;
    QJsonObject row;
    row[lit("id")] = item.id;
    row[lit("role")] = item.role;
    if(!item.visuals.isEmpty())
      row[QLatin1String("visuals")] = item.visuals;
    if(!item.artifacts.isEmpty())
      // i18n-ignore: Persisted conversation JSON field name (Host-verified artifact chips).
      row[lit("artifacts")] = item.artifacts;
    bool historyIncomplete = thread.incompleteHistoryMessageIds.contains(item.id);
    if(!item.attachments.isEmpty())
    {
      bool attachmentsPersisted = false;
      // The shared budget reserves four UTF-8 bytes per text character. Attachments
      // already have an exact compact-JSON byte size (base64 is ASCII), so charging
      // that byte size as characters would reserve four times their actual storage.
      if(chatAttachmentsCouldFitPersistenceBudget(item.attachments, *remainingChars * 4))
      {
        const QJsonArray attachmentRows = chatAttachmentsToJSON(item.attachments);
        const int attachmentBytes =
            QJsonDocument(attachmentRows).toJson(QJsonDocument::Compact).size();
        if(attachmentBytes <= *remainingChars * 4)
        {
          // i18n-ignore: Persisted conversation attachment protocol field.
          row[lit("attachments")] = attachmentRows;
          *remainingChars -= (attachmentBytes + 3) / 4;
          attachmentsPersisted = true;
        }
      }
      if(!attachmentsPersisted)
      {
        // Never silently pretend that a persisted history row still has its source material.
        // i18n-ignore: Persisted conversation attachment-omission count.
        row[lit("attachmentsOmitted")] = item.attachments.size();
        historyIncomplete = true;
      }
    }
    const int textLimit = qMin(MaxConversationMessageChars, *remainingChars);
    row[lit("text")] = item.text.left(textLimit);
    // i18n-ignore: Persisted conversation truncation/lineage protocol fields.
    row[lit("textTruncated")] = item.text.size() > textLimit;
    historyIncomplete |= row.value(lit("textTruncated")).toBool();    // i18n-ignore: persisted conversation truncation protocol field
    row[lit("historyIncomplete")] = historyIncomplete;    // i18n-ignore: persisted conversation lineage protocol field
    *remainingChars -= row.value(lit("text")).toString().size();    // i18n-ignore: persisted conversation text protocol field
    const int provisionalLimit = qMin(MaxConversationMessageChars, qMax(0, *remainingChars));
    row[lit("provisionalText")] = item.provisionalText.left(provisionalLimit);
    *remainingChars -= row.value(lit("provisionalText")).toString().size();
    row[lit("state")] = item.state;
    row[lit("taskId")] = item.taskId;
    row[lit("timestampUnixMs")] = QString::number(item.timestampMs);
    // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
    row[lit("finishedUnixMs")] = QString::number(item.finishedTimestampMs);
    row[lit("activityExpanded")] = item.activityExpanded;
    QJsonArray activities;
    QVector<QJsonObject> selectedActivities;
    const int activityFirst =
        qMax(0, item.activity.size() - MaxStoredConversationActivities);
    for(int j = item.activity.size() - 1; j >= activityFirst && *remainingChars > 0; --j)
    {
      const AIActivityItem &activity = item.activity[j];
      QJsonObject activityRow;
      // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
      activityRow[lit("id")] = activity.id.left(MaxActivityIdChars);
      activityRow[lit("kind")] = activity.kind.left(64);
      const int titleLimit = qMin(1000, qMax(0, *remainingChars));
      activityRow[lit("title")] = activity.title.left(titleLimit);
      *remainingChars -= activityRow.value(lit("title")).toString().size();
      const int storedDetailLimit =
          isLiveTextActivityKind(activity.kind) ? MaxLiveAgentMessageChars : 4000;
      const int detailLimit = qMin(storedDetailLimit, qMax(0, *remainingChars));
      activityRow[lit("detail")] = activity.detail.left(detailLimit);
      *remainingChars -= activityRow.value(lit("detail")).toString().size();
      activityRow[lit("completed")] = activity.completed;
      activityRow[lit("error")] = activity.error;
      // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
      activityRow[lit("timestampUnixMs")] = QString::number(activity.timestampMs);
      // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
      activityRow[lit("finishedUnixMs")] = QString::number(activity.finishedTimestampMs);
      selectedActivities.prepend(activityRow);
    }
    for(const QJsonObject &activityRow : selectedActivities)
      activities.push_back(activityRow);
    row[lit("activity")] = activities;
    selectedMessages.prepend(row);
  }
  for(const QJsonObject &row : selectedMessages)
    messages.push_back(row);
  // Count complete transcript rows which could not be persisted. An incomplete boundary row is
  // retained for display and accounted for when conversationPrompt builds its exact suffix.
  // i18n-ignore: Persisted conversation bounded-history lineage field.
  object[lit("omittedMessageCount")] =
      qMin(1000000,
           thread.omittedMessageCount + qMax(0, eligibleMessageCount - selectedMessages.size()));
  object[lit("messages")] = messages;
  return object;
}

bool AIAssistant::conversationFromJSON(const QJsonObject &object, ConversationThread &thread) const
{
  thread.id = object.value(lit("id")).toString();
  thread.title = object.value(lit("title")).toString().left(256);
  // i18n-ignore: Stable AI connection/model identifiers in the local conversation store.
  thread.profileId = object.value(lit("profileId")).toString();
  // i18n-ignore: Stable AI connection/model identifiers in the local conversation store.
  thread.modelId = object.value(lit("modelId")).toString();
  thread.createdMs = object.value(lit("createdUnixMs")).toString().toLongLong();
  thread.updatedMs = object.value(lit("updatedUnixMs")).toString().toLongLong();
  // i18n-ignore: Persisted conversation bounded-history lineage field.
  thread.omittedMessageCount =
      qBound(0, object.value(lit("omittedMessageCount")).toInt(), 1000000);    // i18n-ignore: persisted conversation bounded-history lineage field
  thread.pinned = object.value(lit("pinned")).toBool();    // i18n-ignore: persisted conversation state protocol field
  thread.unread = object.value(lit("unread")).toBool();
  if(!safeTaskId(thread.id) || !object.value(lit("messages")).isArray())
    return false;
  if(thread.title.isEmpty())
    thread.title = tr("Conversation");
  // i18n-ignore: Persisted ReplayLab clarification protocol field name.
  const QJsonValue clarificationValue = object.value(lit("clarification"));
  if(clarificationValue.isObject())
  {
    ClarificationPrompt clarification;
    if(clarificationFromJSON(clarificationValue.toObject(), thread.id, clarification))
      thread.clarification = clarification;
  }
  const QJsonArray messages = object.value(lit("messages")).toArray();
  const int first = qMax(0, messages.size() - MaxConversationMessages);
  for(int i = first; i < messages.size(); ++i)
  {
    if(!messages[i].isObject())
      continue;
    const QJsonObject row = messages[i].toObject();
    AIChatMessage item;
    item.id = row.value(lit("id")).toString();
    item.role = row.value(lit("role")).toString();
    item.text = row.value(lit("text")).toString().left(MaxConversationMessageChars);
    item.provisionalText =
        row.value(lit("provisionalText")).toString().left(MaxConversationMessageChars);
    item.state = row.value(lit("state")).toString();
    item.taskId = row.value(lit("taskId")).toString();
    item.timestampMs = row.value(lit("timestampUnixMs")).toString().toLongLong();
    // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
    item.finishedTimestampMs = row.value(lit("finishedUnixMs")).toString().toLongLong();
    item.activityExpanded = row.value(lit("activityExpanded")).toBool();
    // i18n-ignore: Persisted conversation JSON field name (Host-verified artifact chips).
    item.artifacts = row.value(lit("artifacts")).toArray();
    const QJsonArray visualRows = row.value(QLatin1String("visuals")).toArray();
    for(int visualIndex = 0; visualIndex < qMin(8, visualRows.size()); ++visualIndex)
    {
      const QString id = visualRows[visualIndex].toObject().value(QLatin1String("id")).toString();
      if(AIVisualReply::validIdentity(id))
        item.visuals.append(QJsonObject{{QLatin1String("id"), id}});
    }
    if(!safeTaskId(item.id) || (item.role != lit("user") && item.role != lit("assistant")))
      continue;
    const QJsonValue attachmentValue = row.value(lit("attachments"));    // i18n-ignore: conversation store field
    const QVector<AIChatAttachment> loadedAttachments =
        chatAttachmentsFromJSON(attachmentValue);
    const bool invalidAttachments =
        attachmentValue.isArray() && !attachmentValue.toArray().isEmpty() &&
        loadedAttachments.isEmpty();
    if(!invalidAttachments)
      item.attachments = loadedAttachments;
    const int omittedAttachments =
        qBound(0, row.value(lit("attachmentsOmitted")).toInt(), MaxChatAttachments);    // i18n-ignore: conversation store field
    if(row.value(lit("historyIncomplete")).toBool() ||    // i18n-ignore: conversation store field
       row.value(lit("textTruncated")).toBool() || invalidAttachments ||    // i18n-ignore: conversation store field
       omittedAttachments > 0)
      thread.incompleteHistoryMessageIds.insert(item.id);
    const bool interruptedOnLoad =
        item.state == lit("queued") || item.state == lit("running") ||
        item.state == lit("planning") || item.state == lit("finalizing");
    if(interruptedOnLoad)
    {
      item.state = lit("interrupted");
      item.activityExpanded = false;
    }
    const QJsonArray activities = row.value(lit("activity")).toArray();
    const int activityFirst =
        qMax(0, activities.size() - MaxStoredConversationActivities);
    for(int j = activityFirst; j < activities.size(); ++j)
    {
      if(!activities[j].isObject())
        continue;
      const QJsonObject activityRow = activities[j].toObject();
      AIActivityItem activity;
      // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
      activity.id = activityRow.value(lit("id")).toString().left(MaxActivityIdChars);
      activity.kind = activityRow.value(lit("kind")).toString().left(64);
      activity.title = activityRow.value(lit("title")).toString().left(1000);
      const int storedDetailLimit =
          isLiveTextActivityKind(activity.kind) ? MaxLiveAgentMessageChars : 4000;
      activity.detail = activityRow.value(lit("detail")).toString().left(storedDetailLimit);    // i18n-ignore: conversation store field
      activity.completed = activityRow.value(lit("completed")).toBool();
      activity.error = activityRow.value(lit("error")).toBool();
      activity.timestampMs =
          // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
          activityRow.value(lit("timestampUnixMs")).toString().toLongLong();
      activity.finishedTimestampMs =
          // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
          activityRow.value(lit("finishedUnixMs")).toString().toLongLong();
      if(interruptedOnLoad && !activity.completed)
      {
        activity.completed = true;
        activity.error = true;
        activity.finishedTimestampMs = QDateTime::currentMSecsSinceEpoch();
      }
      item.activity.push_back(activity);
    }
    if(invalidAttachments || omittedAttachments > 0)
    {
      AIActivityItem unavailable;
      unavailable.kind = lit("attachment-unavailable");    // i18n-ignore: internal activity-kind ID
      unavailable.title = tr("Attachment content unavailable");
      unavailable.detail = invalidAttachments
                               ? tr("Stored attachment data was invalid and was not loaded.")
                               : tr("%1 attachment(s) were not persisted because the bounded "
                                    "local conversation store was full.")
                                     .arg(omittedAttachments);
      unavailable.timestampMs = item.timestampMs;
      unavailable.finishedTimestampMs = item.timestampMs;
      unavailable.completed = true;
      unavailable.error = true;
      item.activity.push_back(unavailable);
    }
    if(interruptedOnLoad)
    {
      AIActivityItem interrupted;
      interrupted.kind = lit("interrupted");
      interrupted.title = tr("Interrupted when the capture closed");
      interrupted.timestampMs = QDateTime::currentMSecsSinceEpoch();
      interrupted.finishedTimestampMs = interrupted.timestampMs;
      interrupted.completed = true;
      item.activity.push_back(interrupted);
      item.finishedTimestampMs = interrupted.timestampMs;
    }
    thread.messages.push_back(item);
  }
  return true;
}

void AIAssistant::loadConversations()
{
  m_LoadingConversations = true;
  m_ConversationStoreReadOnly = false;
  m_Conversations.clear();
  m_ComposerAttachments.clear();
  m_ComposerSystemThumbnails.clear();
  m_ComposerSystemAnimations.clear();
  rebuildAttachmentChips();
  discardAllChatContextFreezes();
  m_ChatQueue.clear();
  m_ActiveConversationId.clear();
  QString storeError;
  bool removedDeletedConversations = false;
  QJsonArray interruptedQueue;
  if(!m_CaptureConversationPath.isEmpty())
  {
    QByteArray encoded;
    // A capture can be closed and reopened before its low-priority disk commit runs. Prefer the
    // newest in-process generation for this normalized key so reopening never hydrates stale disk
    // state, without making the GUI wait for QSaveFile.
    const bool loaded = latestConversationPayload(m_CaptureConversationPath, &encoded) ||
                        AIConversationStore::Load(m_CaptureConversationPath, &encoded,
                                                  &storeError);
    if(loaded)
    {
      if(!encoded.isEmpty())
      {
        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(encoded, &parseError);
        if(parseError.error != QJsonParseError::NoError || !document.isObject())
        {
          storeError = tr("The stored conversation payload is invalid JSON.");
        }
        else
        {
          const QJsonObject root = document.object();
          const QString storeSchema = root.value(lit("schema")).toString();    // i18n-ignore: conversation store field
          if(storeSchema != lit("replaylab-ai-conversations/v2") &&    // i18n-ignore: conversation store schema ID
             storeSchema != lit("replaylab-ai-conversations/v3") &&    // i18n-ignore: conversation store schema ID
             storeSchema != lit("replaylab-ai-conversations/v4") &&    // i18n-ignore: conversation store schema ID
             storeSchema != lit("replaylab-ai-conversations/v5"))    // i18n-ignore: conversation store schema ID
          {
            storeError = tr("The stored conversation payload uses an unsupported schema.");
          }
          else
          {
            m_ActiveConversationId = root.value(lit("activeConversationId")).toString();
            interruptedQueue = root.value(lit("queuedMessages")).toArray();
            const QJsonArray threads = root.value(lit("conversations")).toArray();
            for(int i = 0; i < threads.size() && m_Conversations.size() < MaxConversationThreads;
                ++i)
            {
              if(!threads[i].isObject())
                continue;
              ConversationThread thread;
              if(conversationFromJSON(threads[i].toObject(), thread))
              {
                if(conversationDeletionRequested(taskEventRoot(), thread.id))
                  removedDeletedConversations = true;
                else
                  m_Conversations.push_back(thread);
              }
            }
          }
        }
      }
    }
  }
  if(m_Conversations.isEmpty())
  {
    ConversationThread thread;
    thread.id = lit("conversation-") + newTaskId();
    thread.title = tr("New conversation");
    thread.profileId = m_ActiveProfile;
    thread.modelId = m_ActiveModel;
    thread.createdMs = thread.updatedMs = QDateTime::currentMSecsSinceEpoch();
    m_Conversations.push_back(thread);
    m_ActiveConversationId = thread.id;
  }
  if(!conversation(m_ActiveConversationId))
    m_ActiveConversationId = m_Conversations.back().id;
  const bool repairedConnectionSelections = repairConversationConnectionSelections();
  syncConnectionSelectionFromConversation();
  const int queuedFirst = qMax(0, interruptedQueue.size() - MaxPendingChats);
  for(int i = queuedFirst; i < interruptedQueue.size(); ++i)
  {
    if(!interruptedQueue[i].isObject())
      continue;
    const QJsonObject row = interruptedQueue[i].toObject();
    const QString taskId = row.value(lit("taskId")).toString();
    const QString conversationId = row.value(lit("conversationId")).toString();
    const QString prompt = row.value(lit("prompt")).toString().left(MaxConversationMessageChars);
    const QJsonValue attachmentValue = row.value(lit("attachments"));    // i18n-ignore: conversation store field
    const QVector<AIChatAttachment> loadedAttachments =
        chatAttachmentsFromJSON(attachmentValue);
    const bool invalidAttachments =
        attachmentValue.isArray() && !attachmentValue.toArray().isEmpty() &&
        loadedAttachments.isEmpty();
    const int omittedAttachments =
        qBound(0, row.value(lit("attachmentsOmitted")).toInt(), MaxChatAttachments);    // i18n-ignore: conversation store field
    ConversationThread *thread = conversation(conversationId);
    if(!thread || !safeTaskId(taskId) ||
       (prompt.trimmed().isEmpty() && loadedAttachments.isEmpty() && omittedAttachments == 0))
      continue;
    const QString messageId = lit("message-") + taskId;
    bool duplicate = false;
    for(const AIChatMessage &existing : thread->messages)
      duplicate |= existing.id == messageId;
    if(duplicate)
      continue;
    AIChatMessage item;
    item.id = messageId;
    item.role = lit("user");
    item.text = prompt;
    if(!invalidAttachments)
      item.attachments = loadedAttachments;
    item.taskId = taskId;
    item.state = lit("interrupted");
    item.timestampMs = row.value(lit("queuedUnixMs")).toString().toLongLong();
    if(item.timestampMs <= 0)
      item.timestampMs = thread->updatedMs;
    item.finishedTimestampMs = qMax(item.timestampMs, thread->updatedMs);
    AIActivityItem activity;
    activity.kind = lit("interrupted");
    activity.title = tr("Queued message was not run before the previous session ended");
    QStringList recoveryDetails;
    if(row.value(lit("truncated")).toBool())
      recoveryDetails << tr("The recovery copy was truncated to fit the local history limit.");
    if(invalidAttachments)
      recoveryDetails << tr("Stored attachment data was invalid and was not loaded.");
    else if(omittedAttachments > 0)
      recoveryDetails
          << tr("%1 attachment(s) were not persisted because the bounded local conversation "
                "store was full.")
                 .arg(omittedAttachments);
    activity.detail = recoveryDetails.join(QLatin1Char('\n'));
    activity.timestampMs = item.finishedTimestampMs;
    activity.finishedTimestampMs = item.finishedTimestampMs;
    activity.completed = true;
    item.activity.push_back(activity);
    thread->messages.push_back(item);
    thread->updatedMs = qMax(thread->updatedMs, item.timestampMs);
  }
  if(ConversationThread *active = conversation(m_ActiveConversationId))
    active->unread = false;
  // A store that could not be read is not a store that is empty. The reasons Load()
  // reports include an anti-virus or backup process holding the file, a payload over
  // the size cap and a schema a newer build wrote -- all recoverable, and all of them
  // used to end with the next save replacing the user's whole history with the one
  // blank thread created above. Stay read-only until this capture is loaded again.
  m_ConversationStoreReadOnly = !storeError.isEmpty();
  m_LoadingConversations = false;
  if((repairedConnectionSelections || removedDeletedConversations) && !m_ConversationStoreReadOnly)
    saveConversations();
  refreshConversationList();
  renderConversation();
  if(!storeError.isEmpty())
    appendSystem(tr("The conversation history for this capture could not be loaded: %1 "
                    "Nothing will be written to it in this session, so the stored history "
                    "is not lost; reopen the capture to try again.")
                     .arg(storeError),
                 true);
}

void AIAssistant::saveConversations()
{
  if(m_LoadingConversations || m_CaptureConversationPath.isEmpty())
    return;
  // Never overwrite a store this session could not read (see loadConversations).
  if(m_ConversationStoreReadOnly)
    return;
  // An explicit durability snapshot already includes every pending coalesced mutation. Cancel its
  // trailing timer so Send, terminal, capture-switch and Shutdown paths never encode the same full
  // document again 500 ms later.
  if(m_ConversationSaveTimer && m_ConversationSaveTimer->isActive())
    m_ConversationSaveTimer->stop();
  // Keep a conservative byte margin for UTF-8 conversion, JSON structure and escaping.
  int remainingChars = AIConversationStore::MaximumPayloadBytes / 4 - 8192;
  QJsonObject root;
  root[lit("schema")] = lit("replaylab-ai-conversations/v5");    // i18n-ignore: conversation store field/schema ID
  root[lit("activeConversationId")] = m_ActiveConversationId;
  root[lit("savedUnixMs")] = QString::number(QDateTime::currentMSecsSinceEpoch());
  // Pending prompts have not entered the transcript yet. Give them priority over historical rows
  // in each bounded snapshot; the application-owned writer commits the snapshot asynchronously.
  QJsonArray queuedMessages;
  QVector<const PendingChat *> persistedQueue;
  const int pendingFirst = qMax(0, m_ChatQueue.size() - MaxPendingChats);
  for(int i = pendingFirst; i < m_ChatQueue.size(); ++i)
  {
    const PendingChat &request = m_ChatQueue[i];
    if(safeTaskId(request.taskId) && conversation(request.conversationId) &&
       (!request.prompt.trimmed().isEmpty() || !request.attachments.isEmpty()))
      persistedQueue.push_back(&request);
  }
  int pendingBudget = qMin(MaxConversationMessageChars, remainingChars / 3);
  for(int i = 0; i < persistedQueue.size() && pendingBudget > 0; ++i)
  {
    const PendingChat &request = *persistedQueue[i];
    const int requestsLeft = persistedQueue.size() - i;
    const int promptLimit = qMin(
        MaxConversationMessageChars,
        qMin(qMax(0, remainingChars), qMax(0, pendingBudget / requestsLeft)));
    QJsonObject row;
    row[lit("taskId")] = request.taskId;
    row[lit("conversationId")] = request.conversationId;
    if(!request.attachments.isEmpty())
    {
      bool attachmentsPersisted = false;
      if(chatAttachmentsCouldFitPersistenceBudget(request.attachments, remainingChars * 4))
      {
        const QJsonArray attachmentRows = chatAttachmentsToJSON(request.attachments);
        const int attachmentBytes =
            QJsonDocument(attachmentRows).toJson(QJsonDocument::Compact).size();
        if(attachmentBytes <= remainingChars * 4)
        {
          // i18n-ignore: Persisted pending-chat attachment protocol field.
          row[lit("attachments")] = attachmentRows;
          remainingChars -= (attachmentBytes + 3) / 4;
          attachmentsPersisted = true;
        }
      }
      if(!attachmentsPersisted)
      {
        // i18n-ignore: Persisted pending-chat attachment-omission count.
        row[lit("attachmentsOmitted")] = request.attachments.size();
      }
    }
    row[lit("prompt")] = request.prompt.left(promptLimit);
    row[lit("truncated")] = request.prompt.size() > promptLimit;
    const int usedChars = row.value(lit("prompt")).toString().size();
    pendingBudget -= usedChars;
    remainingChars -= usedChars;
    const ConversationThread *thread = conversation(request.conversationId);
    row[lit("queuedUnixMs")] = QString::number(thread ? thread->updatedMs : 0);
    queuedMessages.push_back(row);
  }
  root[lit("queuedMessages")] = queuedMessages;

  QJsonArray threads;
  QSet<QString> liveConversationIds;
  for(auto it = m_TaskConversationIds.cbegin(); it != m_TaskConversationIds.cend(); ++it)
    if(m_RunningTasks.contains(it.key()))
      liveConversationIds.insert(it.value());
  for(const PendingChat &request : m_ChatQueue)
    liveConversationIds.insert(request.conversationId);
  QVector<const ConversationThread *> orderedThreads;
  orderedThreads.reserve(m_Conversations.size());
  for(const ConversationThread &thread : m_Conversations)
    orderedThreads.push_back(&thread);
  std::stable_sort(
      orderedThreads.begin(), orderedThreads.end(),
      [this, &liveConversationIds](const ConversationThread *a,
                                   const ConversationThread *b) {
        const bool aActive = a->id == m_ActiveConversationId;
        const bool bActive = b->id == m_ActiveConversationId;
        if(aActive != bActive)
          return aActive;
        const bool aLive = liveConversationIds.contains(a->id);
        const bool bLive = liveConversationIds.contains(b->id);
        if(aLive != bLive)
          return aLive;
        if(a->pinned != b->pinned)
          return a->pinned;
        if(a->updatedMs != b->updatedMs)
          return a->updatedMs > b->updatedMs;
        return a->createdMs > b->createdMs;
      });
  const int threadCount = qMin(MaxConversationThreads, orderedThreads.size());
  // Preserve thread identities even when only the newest messages fit. The store merges older
  // messages by identity; an omitted thread would instead be treated as an explicit deletion.
  for(int i = 0; i < threadCount; ++i)
    threads.push_back(conversationToJSON(*orderedThreads[i], &remainingChars));
  root[lit("conversations")] = threads;
  // The QObject-owned model is read and bounded only on the GUI thread. The implicitly shared,
  // immutable document then outlives this panel in a serialized low-priority writer, which
  // performs compact JSON generation, base64, QSaveFile I/O and commit without blocking ordinary
  // qrenderdoc interaction.
  queueConversationDocumentSave(m_CaptureConversationPath, QJsonDocument(root));
}

void AIAssistant::scheduleConversationSave()
{
  if(!m_LoadingConversations && !m_CaptureConversationPath.isEmpty() &&
     m_ConversationSaveTimer)
  {
    // Ordinary UI and streaming mutations are a pure trailing-edge debounce. Explicit durability
    // boundaries snapshot saveConversations() once and cancel this pending timer there.
    m_ConversationSaveTimer->start(500);
  }
}

void AIAssistant::updateCaptureStatus()
{
  m_AnnotateRT->setEnabled(m_Ctx.IsCaptureLoaded());
  if(m_Ctx.IsCaptureLoaded())
  {
    const rdcstr captureFilename = m_Ctx.GetCaptureFilename();
    m_Context->setText(tr("Capture: %1 | current EID %2 | selected EID %3")
                           .arg(QString::fromUtf8(captureFilename.c_str(),
                                                  int(captureFilename.size())))
                           .arg(m_Ctx.CurEvent())
                           .arg(m_Ctx.CurSelectedEvent()));
  }
  else
  {
    m_Context->setText(tr("No capture loaded | general chat is available"));
  }
}

void AIAssistant::showShaderIntentMenu(QWidget *anchor)
{
  if(!m_Ctx.IsCaptureLoaded())
  {
    appendSystem(tr("This action requires an open capture."), true);
    return;
  }

  QString error;
  const QStringList stages = m_Ctx.ReplayLab().AvailableShaderStages(&error);
  if(stages.isEmpty())
  {
    appendSystem(error.isEmpty()
                     ? tr("No stage-scoped shader AI actions are available for this capture.")
                     : error,
                 true);
    return;
  }

  QMenu menu(this);
  for(const QString &stage : stages)
  {
    // Stage names are protocol identifiers and intentionally remain untranslated.
    QAction *action = menu.addAction(stage);
    connect(action, &QAction::triggered, this, [this, stage]() {
      StartCaptureIntent(tr("Use @renderdoc-shader-workbench to reorganize the selected Shader. "
                            "Follow the skill workflow and update its Shader Workspace source "
                            "and explanation, including any existing organization."), stage,
                         lit("reverse-edit-selected-shader"));    // i18n-ignore: stable UI intent identifier
    });
  }

  const QPoint position = anchor ? anchor->mapToGlobal(QPoint(0, anchor->height()))
                                 : QCursor::pos();
  menu.exec(position);
}

QString AIAssistant::findAgentCLI() const
{
  // A portable distribution must never pick a source venv, PATH executable or another install.
  const QDir application(QCoreApplication::applicationDirPath());
  const QString bundled = application.filePath(
      lit("replaylab/replaylab-ai.exe"));    // i18n-ignore: fixed first-party executable path
  if(application.exists(lit("RenderDocBread.portable")))    // i18n-ignore: portable package marker
  {
    return QFileInfo(bundled).isFile() ? bundled : QString();
  }
  const QString configured = environmentValue("REPLAYLAB_AI_CLI").trimmed();
  if(!configured.isEmpty() && QFileInfo(configured).isFile())
    return QFileInfo(configured).absoluteFilePath();

  // A deployed runtime belongs to this application. Keep an explicit override for source tests.
  // i18n-ignore: fixed manifest path for the runtime deployed beside the GUI.
  if(application.exists(lit("replaylab/replaylab-runtime-manifest.json")) &&
     QFileInfo(bundled).isFile())
    return bundled;

  const QString appDir = QCoreApplication::applicationDirPath();
  QStringList candidates;
#if defined(Q_OS_WIN)
#if !defined(RELEASE)
  // An undeployed development checkout can still run the source environment.
  candidates << QDir(appDir).filePath(lit("../../replaylab/.venv/Scripts/replaylab-ai.exe"));
#endif
  candidates << QDir(appDir).filePath(lit("replaylab-ai.exe"));
  candidates << QDir(appDir).filePath(lit("replaylab/replaylab-ai.exe"));
#if defined(RELEASE)
  candidates << QDir(appDir).filePath(lit("../../replaylab/.venv/Scripts/replaylab-ai.exe"));    // i18n-ignore: internal fallback executable path
#endif
#else
#if !defined(RELEASE)
  candidates << QDir(appDir).filePath(lit("../../replaylab/.venv/bin/replaylab-ai"));
#endif
  candidates << QDir(appDir).filePath(lit("replaylab-ai"));
  candidates << QDir(appDir).filePath(lit("replaylab/replaylab-ai"));
#if defined(RELEASE)
  candidates << QDir(appDir).filePath(lit("../../replaylab/.venv/bin/replaylab-ai"));    // i18n-ignore: internal fallback executable path
#endif
#endif
  for(const QString &candidate : candidates)
  {
    QFileInfo info(QDir::cleanPath(candidate));
    if(info.isFile())
      return info.absoluteFilePath();
  }

  return QStandardPaths::findExecutable(lit("replaylab-ai"));
}

QString AIAssistant::findBridgeInstance() const
{
  QString instanceId;
  findBridgeBinding(&instanceId, NULL);
  return instanceId;
}

bool AIAssistant::findBridgeBinding(QString *instanceId, QString *captureSessionId) const
{
  if(instanceId)
    instanceId->clear();
  if(captureSessionId)
    captureSessionId->clear();

  QString local = environmentValue("LOCALAPPDATA");
  if(local.isEmpty())
    local = environmentValue("XDG_DATA_HOME");
  if(local.isEmpty())
    // Same per-user data home the Python side derives; never the shared temp directory.
    local = QDir::home().filePath(lit(".local/share"));    // i18n-ignore: fixed path
  QDir instances(QDir(local).filePath(lit("ReplayLab/ipc-v2/instances")));
  if(!instances.exists())
    return false;

  QString bestId;
  QString bestSessionId;
  qint64 bestHeartbeat = 0;
  const qint64 now = QDateTime::currentMSecsSinceEpoch();
  const qint64 currentPid = QCoreApplication::applicationPid();
  const QFileInfoList directories =
      instances.entryInfoList(QStringList{QString::number(currentPid) + QLatin1String("-*")},
                              QDir::Dirs | QDir::NoDotAndDotDot, QDir::Time);
  for(const QFileInfo &directory : directories)
  {
    QFile manifest(QDir(directory.absoluteFilePath()).filePath(lit("instance.json")));
    if(!manifest.open(QIODevice::ReadOnly) || manifest.size() <= 0 ||
       manifest.size() > MaxManifestBytes)
      continue;
    const QJsonDocument document = QJsonDocument::fromJson(manifest.readAll());
    if(!document.isObject())
      continue;
    const QJsonObject object = document.object();
    const qint64 pid = object.value(lit("pid")).toVariant().toLongLong();
    const qint64 heartbeat = object.value(lit("heartbeatUnixMs")).toVariant().toLongLong();
    // i18n-ignore: ReplayLab bridge manifest instance identifier field.
    const QString manifestInstanceId = object.value(lit("instanceId")).toString();
    // i18n-ignore: ReplayLab bridge manifest capture-session identifier field.
    const QString sessionId = object.value(lit("captureSessionId")).toString();
    if(object.value(lit("schema")).toString() != lit("replaylab-bridge-instance/v1") ||
       object.value(lit("protocolVersion")).toInt() != 2 ||
       directory.fileName() != manifestInstanceId || pid != currentPid ||
       object.value(lit("stopped")).toBool() ||
       // i18n-ignore: ReplayLab bridge manifest capture-loaded state field.
       !object.value(lit("captureLoaded")).toBool() || !safeTaskId(manifestInstanceId) ||
       !isLowerHexIdentifier(sessionId, 32) ||
       heartbeat <= 0 || heartbeat > now + 5000 || now - heartbeat > 5000)
      continue;
    if(heartbeat > bestHeartbeat)
    {
      bestHeartbeat = heartbeat;
      bestId = manifestInstanceId;
      bestSessionId = sessionId;
    }
  }
  if(bestId.isEmpty())
    return false;
  if(instanceId)
    *instanceId = bestId;
  if(captureSessionId)
    *captureSessionId = bestSessionId;
  return true;
}

QString AIAssistant::taskEventRoot() const
{
  QString local = environmentValue("LOCALAPPDATA");
  if(local.isEmpty())
    local = environmentValue("XDG_DATA_HOME");
  if(local.isEmpty())
    // Same per-user data home the agent writes the task event stream under.
    local = QDir::home().filePath(lit(".local/share"));    // i18n-ignore: fixed path
  return QDir(local).filePath(lit("ReplayLab/ai-tasks"));
}

QString AIAssistant::activeConversationTaskId() const
{
  const ConversationThread *thread = conversation();
  if(!thread)
    return QString();
  // A running request in the selected conversation is the most precise live target. Never use a
  // task from another conversation merely because it was started first.
  const ChatExecution *execution = conversationExecution(thread->id);
  if(execution && safeTaskId(execution->request.taskId))
    return execution->request.taskId;
  // Queued requests already own durable task ids before their user message is appended to the
  // conversation. Prefer the newest queued turn for the chat currently visible to the user.
  for(int i = m_ChatQueue.size() - 1; i >= 0; --i)
  {
    const PendingChat &request = m_ChatQueue[i];
    if(request.conversationId == thread->id && safeTaskId(request.taskId))
      return request.taskId;
  }
  // Completed mappings are deliberately released, but the durable conversation rows retain the
  // exact task id. Reverse order chooses the latest turn in the chat currently visible to the user.
  for(int i = thread->messages.size() - 1; i >= 0; --i)
  {
    const QString taskId = thread->messages[i].taskId;
    if(safeTaskId(taskId))
      return taskId;
  }
  return QString();
}

AIAssistant::ChatExecution *AIAssistant::chatExecution(const QString &taskId) const
{
  return m_ChatExecutions.value(taskId, NULL);
}

AIAssistant::ChatExecution *AIAssistant::chatExecution(QProcess *process) const
{
  if(!process)
    return NULL;
  for(auto it = m_ChatExecutions.cbegin(); it != m_ChatExecutions.cend(); ++it)
    if(it.value() && it.value()->process == process)
      return it.value();
  return NULL;
}

AIAssistant::ChatExecution *AIAssistant::conversationExecution(
    const QString &conversationId) const
{
  if(conversationId.isEmpty())
    return NULL;
  for(auto it = m_ChatExecutions.cbegin(); it != m_ChatExecutions.cend(); ++it)
    if(it.value() && it.value()->request.conversationId == conversationId)
      return it.value();
  return NULL;
}

void AIAssistant::configureProcess(QProcess *process, const QString &credentialProfile,
                                   void **processInformation) const
{
  QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
  const QProcessEnvironment inherited = environment;

  // A qrenderdoc session can hold credentials for several model APIs. Give each AgentHost process
  // only the key selected by its exact profile; managed subscription profiles receive none of
  // these model credentials. The subscription connector applies a second process allowlist.
  QSet<QString> credentialNames = knownModelCredentialEnvironments();
  for(auto it = m_ProfileKeyEnvs.cbegin(); it != m_ProfileKeyEnvs.cend(); ++it)
    if(!it.value().isEmpty())
      credentialNames.insert(it.value().toUpper());
  for(const QString &name : environment.keys())
    if(credentialNames.contains(name.toUpper()))
      environment.remove(name);

  const QString selectedName = m_ProfileKeyEnvs.value(credentialProfile);
  if(!selectedName.isEmpty())
  {
    if(m_SessionSecrets.contains(credentialProfile))
      environment.insert(selectedName, m_SessionSecrets.value(credentialProfile));
    else if(inherited.contains(selectedName))
      environment.insert(selectedName, inherited.value(selectedName));
  }

  // ReplayLab's stdout/stderr and JSONL protocols are UTF-8. On Windows, a
  // redirected Python process otherwise selects the active ANSI code page
  // (commonly CP936 on Chinese systems), while this class deliberately decodes
  // all process output with QString::fromUtf8. Force a deterministic encoding
  // so localized failures and non-ASCII capture/profile/workspace paths survive
  // the QProcess boundary. Non-Python Codex commands safely ignore these.
  environment.insert(lit("PYTHONUTF8"), lit("1"));
  environment.insert(lit("PYTHONIOENCODING"), lit("utf-8:strict"));
  // The current user's natural-language question remains the first response-language signal.
  // Identifier-only requests fall back to qrenderdoc's configured UI language, with the real
  // system locale used when the UI setting follows the system. This is Host metadata, not text
  // injected into the visible conversation.
  QString responseLocale = m_Ctx.Config().UILanguage.trimmed();
  if(responseLocale.isEmpty() ||
     responseLocale.compare(lit("system"), Qt::CaseInsensitive) == 0)    // i18n-ignore: stable UI-language configuration value
    responseLocale = QLocale::system().name();
  // i18n-ignore: Fixed AgentHost process-environment protocol key; never shown to users.
  environment.insert(lit("REPLAYLAB_RESPONSE_LOCALE"), responseLocale);
  // AgentHost executes the first-party workflow core in-process. Give its
  // process-owner bootstrap the fixed packaged tool root; development builds
  // may intentionally run the Python CLI from the repository venv, which is
  // not where qrenderdoc deploys the Shader binaries.
  environment.insert(
      // i18n-ignore: Fixed process-environment protocol key; it is never shown to users.
      lit("REPLAYLAB_SHADER_RUNTIME_DIR"),
      // i18n-ignore: Fixed packaged runtime directory name; it is never shown to users.
      QDir(QCoreApplication::applicationDirPath()).filePath(lit("replaylab")));
  process->setProcessEnvironment(environment);
#if defined(Q_OS_WIN)
  process->setCreateProcessArgumentsModifier(
      [processInformation](QProcess::CreateProcessArguments *args) {
    // CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS | CREATE_SUSPENDED. The primary thread is
    // released only after attachKillOnCloseJob() has contained this exact process, closing the
    // interval in which a fast host could otherwise create an uncontained provider/tool child.
    // Descendants inherit the lower scheduling class, so interactive qrenderdoc work wins whenever
    // both process trees want CPU time.
    args->flags |= 0x08000000UL | 0x00004000UL;
    if(processInformation)
    {
      args->flags |= 0x00000004UL;
      *processInformation = args->processInformation;
    }
  });
#else
  Q_UNUSED(processInformation);
#endif
}

bool AIAssistant::attachKillOnCloseJob(QProcess *process, void *&jobHandle, QString *error) const
{
  if(error)
    error->clear();
  closeKillOnCloseJob(jobHandle);

#if defined(Q_OS_WIN)
  if(!process || process->state() == QProcess::NotRunning || process->processId() == 0)
  {
    if(error)
      *error = tr("The AI host did not expose a running process for containment.");
    return false;
  }

  HANDLE job = CreateJobObjectW(NULL, NULL);
  if(!job)
  {
    if(error)
      *error = tr("Could not create an AI process job (Windows error %1).")
                   .arg(qulonglong(GetLastError()));
    return false;
  }

  JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {};
  limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
  if(!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits)))
  {
    const DWORD code = GetLastError();
    CloseHandle(job);
    if(error)
      *error = tr("Could not configure the AI process job (Windows error %1).")
                   .arg(qulonglong(code));
    return false;
  }

  HANDLE child = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE |
                                 PROCESS_QUERY_LIMITED_INFORMATION,
                             FALSE, DWORD(process->processId()));
  if(!child)
  {
    const DWORD code = GetLastError();
    CloseHandle(job);
    if(error)
      *error = tr("Could not open the AI host for containment (Windows error %1).")
                   .arg(qulonglong(code));
    return false;
  }

  const BOOL assigned = AssignProcessToJobObject(job, child);
  const DWORD assignError = assigned ? ERROR_SUCCESS : GetLastError();
  if(!assigned)
  {
    CloseHandle(child);
    CloseHandle(job);
    if(error)
      *error = tr("Could not contain the AI host process tree (Windows error %1).")
                   .arg(qulonglong(assignError));
    return false;
  }

  CloseHandle(child);
  jobHandle = job;
#else
  Q_UNUSED(process);
  if(error)
    *error = tr("The AI runtime currently requires Windows process-tree containment.");
  return false;
#endif
  return true;
}

bool AIAssistant::resumeSuspendedProcess(void *processInformation, QString *error) const
{
  if(error)
    error->clear();
#if defined(Q_OS_WIN)
  PROCESS_INFORMATION *information = static_cast<PROCESS_INFORMATION *>(processInformation);
  if(!information || !information->hThread)
  {
    if(error)
      *error = tr("Could not contain the AI host process tree (Windows error %1).")
                   .arg(qulonglong(ERROR_INVALID_HANDLE));
    return false;
  }
  if(ResumeThread(information->hThread) == DWORD(-1))
  {
    const DWORD code = GetLastError();
    if(error)
      *error = tr("Could not contain the AI host process tree (Windows error %1).")
                   .arg(qulonglong(code));
    return false;
  }
  return true;
#else
  Q_UNUSED(processInformation);
  if(error)
    *error = tr("The AI runtime currently requires Windows process-tree containment.");
  return false;
#endif
}

void AIAssistant::closeKillOnCloseJob(void *&jobHandle) const
{
#if defined(Q_OS_WIN)
  if(jobHandle)
    CloseHandle(static_cast<HANDLE>(jobHandle));
#endif
  jobHandle = NULL;
}

void AIAssistant::runCLIAsync(const QStringList &arguments, const QByteArray &input, int timeoutMs,
                             QObject *owner, const CLICompletion &completion,
                             const QString &credentialProfile,
                             const std::function<void(const QByteArray &)> &outputProgress)
{
  const QPointer<AIAssistant> assistant(this);
  const QPointer<QObject> ownerGuard(owner);
  if(owner && AIConfiguration::handles(arguments))
  {
    // Configuration and OS credentials are local operations. They must remain available even
    // when AgentHost is missing, slow to unpack, or unable to start.
    QTimer::singleShot(0, owner, [assistant, ownerGuard, arguments, input, completion]() {
      if(!assistant || !ownerGuard || assistant->m_ShuttingDown)
        return;
      const AIConfiguration::Reply reply = AIConfiguration::execute(arguments, input);
      completion(reply.exitCode, reply.output, reply.diagnostic, false);
    });
    return;
  }
  if(!owner || m_CLIPath.isEmpty())
  {
    const QString diagnostic = tr("AI host was not found");
    if(owner)
      QTimer::singleShot(0, owner, [assistant, ownerGuard, completion, diagnostic]() {
        if(assistant && ownerGuard && !assistant->m_ShuttingDown)
          completion(-1, QByteArray(), diagnostic, true);
      });
    return;
  }
  if(input.size() > MaxPromptBytes)
  {
    const QString diagnostic = tr("Command input exceeds the 16 MiB safety limit");
    QTimer::singleShot(0, owner, [assistant, ownerGuard, completion, diagnostic]() {
      if(assistant && ownerGuard && !assistant->m_ShuttingDown)
        completion(-1, QByteArray(), diagnostic, false);
    });
    return;
  }

  QProcess *process = new QProcess(owner);
  std::shared_ptr<AIConfigurationProcessState> state =
      std::make_shared<AIConfigurationProcessState>();
  configureProcess(process, credentialProfile, &state->processInformation);
  // Generic one-shot commands use a protocol gate as a second containment boundary. AgentHost
  // consumes exactly this first stdin record before command dispatch, then preserves the existing
  // protected payload unchanged. Subscription and task workers own separate gates and do not pass
  // through runCLIAsync.
  QProcessEnvironment gatedEnvironment = process->processEnvironment();
  // i18n-ignore: Fixed first-party process-environment protocol key; never shown to users.
  gatedEnvironment.insert(lit("REPLAYLAB_START_GATED"), lit("1"));
  process->setProcessEnvironment(gatedEnvironment);
  const QString cliPath = m_CLIPath;
  state->assistant = this;
  state->owner = owner;
  state->process = process;
  state->completion = completion;
  state->input = input;
  state->timeoutMs = qMax(1000, timeoutMs);
  // Only fixed protocol verbs may reach the log. Arguments can contain sensitive values.
  state->operation = QStringLiteral("other");
  const QString verb = arguments.value(0);
  const QString subcommand = arguments.value(1);
  if(verb == QStringLiteral("profile") && subcommand == QStringLiteral("subscription"))
    state->operation = QStringLiteral("profile subscription");
  else if(verb == QStringLiteral("profile") && subcommand == QStringLiteral("models"))
    state->operation = QStringLiteral("profile models");
  else if(verb == QStringLiteral("tools"))
    state->operation = QStringLiteral("tools");
  else if(verb == QStringLiteral("doctor"))
    state->operation = QStringLiteral("doctor");
  QTimer *timeout = new QTimer(process);
  timeout->setSingleShot(true);
  state->timeout = timeout;
  m_ConfigurationProcesses.insert(process, state);
  // QWidget destroys child widgets/processes before QObject clears its guarded
  // pointers. QProcess::~QProcess can emit finished during that interval. Retire
  // callbacks when a dialog finishes, before any of its controls are destroyed.
  for(QObject *ancestor = owner; ancestor; ancestor = ancestor->parent())
  {
    if(QDialog *ownerDialog = qobject_cast<QDialog *>(ancestor))
    {
      connect(ownerDialog, &QDialog::finished, owner, [state](int) {
        if(state->completed)
          return;
        state->log("cancelled");
        state->completed = true;
        state->completion = CLICompletion();
        if(state->timeout)
          state->timeout->stop();
        state->closeJob();
        if(state->process)
          state->process->kill();
      });
    }
  }
  connect(process, &QObject::destroyed, this, [this, process]() {
    const std::shared_ptr<AIConfigurationProcessState> state =
        m_ConfigurationProcesses.take(process);
    if(!state)
      return;
    if(!state->completed)
      state->log("owner destroyed");
    state->completed = true;
    state->completion = std::function<void(int, const QByteArray &, const QString &, bool)>();
    state->closeJob();
  });

  std::shared_ptr<std::function<void(bool)>> finish =
      std::make_shared<std::function<void(bool)>>();
  *finish = [state, outputProgress](bool unavailable) {
    if(state->completed)
      return;
    state->completed = true;
    if(state->timeout)
      state->timeout->stop();

    int exitCode = -1;
    QByteArray standardOutput;
    QString diagnostic;
    QProcess *selectedProcess = state->process.data();
    if(selectedProcess)
    {
      const QByteArray outputTail = selectedProcess->readAllStandardOutput();
      if(state->standardOutput.size() + outputTail.size() > MaxProcessOutputBytes)
        state->forcedDiagnostic =
            QCoreApplication::translate("AIAssistant", "Command output exceeded the safety limit");
      else
        state->standardOutput += outputTail;
      if(outputProgress && !outputTail.isEmpty())
        outputProgress(outputTail);
      const QByteArray errorTail = selectedProcess->readAllStandardError();
      const int remainingError = qMax(0, MaxEventLineBytes - state->standardError.size());
      state->standardError += errorTail.left(remainingError);
      const bool normal = selectedProcess->exitStatus() == QProcess::NormalExit;
      exitCode = normal ? selectedProcess->exitCode() : -1;
      standardOutput = state->standardOutput;
      diagnostic = QString::fromUtf8(state->standardError).trimmed();
      if(diagnostic.isEmpty() && (!normal || exitCode != 0))
        diagnostic =
            normal ? QCoreApplication::translate("AIAssistant",
                                                 "Command failed with exit code %1")
                         .arg(exitCode)
                   : QCoreApplication::translate("AIAssistant",
                                                 "Command terminated unexpectedly");
    }
    if(!state->forcedDiagnostic.isEmpty())
    {
      diagnostic = state->forcedDiagnostic;
      exitCode = -1;
    }
    state->log(unavailable ? "start failed" : "finished", exitCode);
    const QPointer<AIAssistant> assistant = state->assistant;
    const QPointer<QObject> owner = state->owner;
    const std::function<void(int, const QByteArray &, const QString &, bool)> completion =
        std::move(state->completion);
    if(assistant && selectedProcess)
      assistant->m_ConfigurationProcesses.remove(selectedProcess);
    state->closeJob();
    if(state->process)
      state->process->deleteLater();
    // The Settings page and the assistant have independent lifetimes. A callback commonly captures
    // both, so never invoke it unless both guards are still valid and Shutdown has not begun.
    if(assistant && owner && !assistant->m_ShuttingDown && completion)
      completion(exitCode, standardOutput, diagnostic, unavailable);
  };

  connectAIConfigurationSignal(process, &QProcess::started, owner,
                               [assistant, state]() {
    if(state->completed || !state->process)
      return;
    state->log("process started");
    if(!assistant)
    {
      state->forcedDiagnostic =
          QCoreApplication::translate("AIAssistant", "AI host was not found");
      state->process->kill();
      return;
    }
    QString containmentError;
    void *job = NULL;
    if(!assistant->attachKillOnCloseJob(state->process, job, &containmentError))
    {
      state->forcedDiagnostic = containmentError;
      state->process->kill();
      return;
    }
    state->job = job;
    static const QByteArray startGate("start\n");
    if(state->process->write(startGate) != startGate.size())
    {
      state->forcedDiagnostic =
          QCoreApplication::translate("AIAssistant", "Could not send protected command input");
      state->process->kill();
      return;
    }
    if(!state->input.isEmpty() && state->process->write(state->input) != state->input.size())
    {
      state->forcedDiagnostic =
          QCoreApplication::translate("AIAssistant", "Could not send protected command input");
      state->process->kill();
      return;
    }
    state->process->closeWriteChannel();
    if(!assistant->resumeSuspendedProcess(state->processInformation, &containmentError))
    {
      state->forcedDiagnostic = containmentError;
      state->closeJob();
      state->process->kill();
    }
  });
  connectAIConfigurationSignal(process, &QProcess::readyReadStandardOutput, owner, [state, outputProgress]() {
    if(state->completed || !state->process)
      return;
    const QByteArray chunk = state->process->readAllStandardOutput();
    if(state->standardOutput.size() + chunk.size() > MaxProcessOutputBytes)
    {
      state->forcedDiagnostic =
          QCoreApplication::translate("AIAssistant", "Command output exceeded the safety limit");
      state->process->kill();
      return;
    }
    state->standardOutput += chunk;
    if(outputProgress)
      outputProgress(chunk);
  });
  connectAIConfigurationSignal(process, &QProcess::readyReadStandardError, owner, [state]() {
    if(state->completed || !state->process)
      return;
    const QByteArray chunk = state->process->readAllStandardError();
    const int remaining = qMax(0, MaxEventLineBytes - state->standardError.size());
    state->standardError += chunk.left(remaining);
  });
  connectAIConfigurationSignal(
      process, static_cast<void (QProcess::*)(int, QProcess::ExitStatus)>(&QProcess::finished),
      owner, [finish](int, QProcess::ExitStatus) { (*finish)(false); });
  connectAIConfigurationSignal(process, &QProcess::errorOccurred, owner,
                               [state, finish](QProcess::ProcessError error) {
    if(error != QProcess::FailedToStart || state->completed)
      return;
    if(state->process)
      state->forcedDiagnostic = state->process->errorString();
    (*finish)(true);
  });
  connectAIConfigurationSignal(timeout, &QTimer::timeout, owner, [state, finish]() {
    if(state->completed)
      return;
    state->log("timed out");
    state->forcedDiagnostic = QCoreApplication::translate("AIAssistant", "Command timed out");
    if(state->process)
      state->process->kill();
    (*finish)(false);
  });

  state->elapsed.start();
  state->log("starting");
  timeout->start(state->timeoutMs);
  process->start(cliPath, arguments, QIODevice::ReadWrite);
}

void AIAssistant::runConnectionCheck(QWidget *parent, const QString &profile, bool executionTest,
                                      const std::function<void()> &completion)
{
  auto view = std::make_shared<LiveConnectionDiagnostics>();
  QDialog *dialog = new QDialog(parent);
  dialog->setObjectName(QStringLiteral("AIConnectionCheckDialog"));
  view->dialog = dialog;
  dialog->setAttribute(Qt::WA_DeleteOnClose);
  dialog->setWindowTitle(tr("AI Connection Check"));
  dialog->resize(820, 520);
  QVBoxLayout *layout = new QVBoxLayout(dialog);
  view->summary = new QLabel(tr("Checking the connection. Close this window to cancel."), dialog);
  view->summary->setObjectName(QStringLiteral("AIConnectionCheckStatus"));
  view->summary->setWordWrap(true);
  layout->addWidget(view->summary);
  view->checks = new QTreeWidget(dialog);
  view->checks->setObjectName(QStringLiteral("AIConnectionCheckRows"));
  view->checks->setColumnCount(3);
  view->checks->setHeaderLabels({tr("Status"), tr("Check"), tr("Detail")});
  view->checks->setRootIsDecorated(false);
  view->checks->setAlternatingRowColors(true);
  view->checks->header()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
  view->checks->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
  view->checks->header()->setSectionResizeMode(2, QHeaderView::Stretch);
  layout->addWidget(view->checks, 1);
  view->detail = new QTextBrowser(dialog);
  view->detail->setMaximumHeight(120);
  view->detail->setOpenExternalLinks(false);
  layout->addWidget(view->detail);
  connect(view->checks, &QTreeWidget::currentItemChanged, dialog,
          [view](QTreeWidgetItem *item, QTreeWidgetItem *) {
    view->detail->setPlainText(item ? item->data(2, Qt::UserRole).toString() : QString());
  });
  auto buttons = new QDialogButtonBox(QDialogButtonBox::Close, dialog);
  auto copy = buttons->addButton(tr("Copy All Details"), QDialogButtonBox::ActionRole);
  connect(copy, &QPushButton::clicked, dialog, [view]() {
    QStringList lines;
    for(auto item : view->rows)
      lines << item->text(0) + QStringLiteral(" | ") + item->text(1) + QStringLiteral(" | ") + item->text(2);
    QApplication::clipboard()->setText(lines.join(QLatin1Char('\n')));
  });
  connect(buttons, &QDialogButtonBox::rejected, dialog, &QDialog::reject);
  layout->addWidget(buttons);
  const QPointer<QWidget> page(parent);
  auto notified = std::make_shared<bool>(false);
  auto notify = [page, completion, notified]() {
    if(!*notified)
    {
      *notified = true;
      if(page && completion)
        completion();
    }
  };
  connect(dialog, &QDialog::finished, dialog, [view, notify](int) {
    view->dialog = NULL;
    notify();
  });
  dialog->show();
  // i18n-ignore: fixed CLI command and option identifiers.
  QStringList arguments = {lit("doctor"), lit("--profile"), profile, lit("--jsonl")};
  if(executionTest)
    arguments << QStringLiteral("--execution-test");
  runCLIAsync(arguments, QByteArray(), executionTest ? 360000 : 30000, dialog,
      [view, notify](int exitCode, const QByteArray &, const QString &error, bool) {
    notify();
    if(!view->dialog)
      return;
    DiagnosticLevel overall = exitCode == 0 ? DiagnosticLevel::Success : DiagnosticLevel::Error;
    for(auto item : view->rows)
    {
      const DiagnosticLevel level = diagnosticLevel(item->data(0, Qt::UserRole).toString());
      if(int(level) > int(overall))
        overall = level;
    }
    if(!view->receivedComplete)
    {
      QJsonObject row;
      // i18n-ignore: diagnostic protocol fields, not UI labels.
      row[QStringLiteral("status")] = QStringLiteral("error");
      row[QStringLiteral("name")] = QStringLiteral("process");
      row[QStringLiteral("detail")] = error.isEmpty()
          ? QCoreApplication::translate("AIAssistant", "The diagnostic command did not complete successfully.") : error;
      view->row(1024, row);
      overall = DiagnosticLevel::Error;
    }
    setDiagnosticStatus(view->summary, overall, overall == DiagnosticLevel::Error
        ? QCoreApplication::translate("AIAssistant", "One or more checks failed. Review the details.")
        : overall == DiagnosticLevel::Warning
        ? QCoreApplication::translate("AIAssistant", "Checks completed with warnings or skipped items.")
        : QCoreApplication::translate("AIAssistant", "All reported checks passed."));
    view->dialog->setWindowIcon(diagnosticIcon(view->dialog, overall));
  }, profile, [view](const QByteArray &chunk) { view->consume(chunk); });
}

void AIAssistant::refreshProfiles(QObject *completionOwner,
                                  const std::function<void()> &completion)
{
  if(completionOwner && completion)
    m_ProfileRefreshCompletions.push_back({completionOwner, completion});
  m_CLIPath = findAgentCLI();
  const quint64 generation = ++m_ProfileRefreshGeneration;
  m_ProfileComboLoading = true;
  if(m_ProfileRefreshRunning)
  {
    m_ProfileRefreshPending = true;
    return;
  }
  m_ProfileRefreshRunning = true;
  m_ProfileRefreshPending = false;
  const QString previousProfile = m_ActiveProfile;
  const QString previousModel = m_ActiveModel;
  // A newly deployed frozen AgentHost can take several seconds to unpack on a cold start.
  // Keep this asynchronous, but do not mistake startup latency for an empty connection store.
  runCLIAsync(
      // i18n-ignore: Fixed AgentHost CLI command and option identifiers.
      {lit("profile"), lit("list"), lit("--json")}, QByteArray(), 30000, this,
      [this, generation, previousProfile, previousModel](int exitCode, const QByteArray &output,
                                                         const QString &error, bool) {
        if(generation != m_ProfileRefreshGeneration)
        {
          m_ProfileRefreshRunning = false;
          if(m_ProfileRefreshPending)
          {
            m_ProfileRefreshPending = false;
            refreshProfiles();
          }
          else
          {
            m_ProfileComboLoading = false;
          }
          return;
        }

        m_ProfileRefreshRunning = false;
        QSignalBlocker blocker(m_ProfileCombo);
        m_ProfileCombo->clear();
        m_ProfileCombo->setToolTip(QString());
        m_ActiveProfile.clear();
        m_ActiveKeyEnv.clear();
        m_ActiveModel.clear();
        m_ProfileKeyEnvs.clear();
        m_ProfileModels.clear();
        m_ProfileDefaultModels.clear();
        m_ProfileRunInputSchemas.clear();
        m_ProfileRows = QJsonArray();

        const QJsonDocument document = QJsonDocument::fromJson(output);
        if(exitCode == 0 && document.isArray())
        {
          int persistedDefaultIndex = -1;
          int previousIndex = -1;
          const QJsonArray rows = document.array();
          for(const QJsonValue &value : rows)
          {
            const QJsonObject row = value.toObject();
            if(!supportedQRenderDocConnection(row))
              continue;
            m_ProfileRows.append(row);
            const QString id =
                row.value(lit("id")).toString();    // i18n-ignore: provider protocol key
            if(id.isEmpty())
              continue;
            const QString kind = row.value(lit("kind")).toString();
            const QString loopOwner = row.value(lit("loopOwner")).toString();
            const QString keyEnvironment = row.value(lit("apiKeyEnv")).toString();
            m_ProfileKeyEnvs[id] = keyEnvironment;
            QStringList enabledModels;
            for(const QJsonValue &modelValue : row.value(lit("models")).toArray())
              if(!modelValue.toString().trimmed().isEmpty())
                enabledModels << modelValue.toString().trimmed();
            const QString defaultModel =
                row.value(lit("defaultModel")).toString(row.value(lit("model")).toString());
            if(enabledModels.isEmpty() && !defaultModel.isEmpty())
              enabledModels << defaultModel;
            m_ProfileModels[id] = enabledModels;
            m_ProfileDefaultModels[id] = defaultModel;
            QStringList runInputSchemas;
            for(const QJsonValue &schemaValue :
                row.value(lit("runInputSchemas")).toArray())    // i18n-ignore: AgentHost JSON field name
              if(!schemaValue.toString().trimmed().isEmpty())
                runInputSchemas << schemaValue.toString().trimmed();
            m_ProfileRunInputSchemas[id] = runInputSchemas;
            const QString displayName = connectionDisplayName(row);
            // i18n-ignore: AgentHost connection protocol identifiers.
            const QString label = loopOwner == lit("replaylab") || kind == lit("model")
                                      ? tr("%1 | API").arg(displayName)
                                      : tr("%1 | Subscription").arg(displayName);
            m_ProfileCombo->addItem(label, id);
            m_ProfileCombo->setItemData(m_ProfileCombo->count() - 1, keyEnvironment,
                                        Qt::UserRole + 1);
            if(id == previousProfile)
              previousIndex = m_ProfileCombo->count() - 1;
            if(row.value(lit("active")).toBool())
              persistedDefaultIndex = m_ProfileCombo->count() - 1;
          }
          int selectedIndex = previousIndex >= 0 ? previousIndex : persistedDefaultIndex;
          if(selectedIndex < 0 && m_ProfileCombo->count() > 0)
            selectedIndex = 0;
          if(selectedIndex >= 0)
          {
            m_ProfileCombo->setCurrentIndex(selectedIndex);
            m_ActiveProfile = m_ProfileCombo->currentData().toString();
            m_ActiveKeyEnv = m_ProfileCombo->currentData(Qt::UserRole + 1).toString();
            if(m_ActiveProfile == previousProfile &&
               m_ProfileModels.value(m_ActiveProfile).contains(previousModel))
              m_ActiveModel = previousModel;
            else
              m_ActiveModel = m_ProfileDefaultModels.value(m_ActiveProfile);
          }
        }
        if(m_ProfileCombo->count() == 0)
        {
          m_ProfileCombo->addItem(tr("No connection configured"), QString());
          if(!error.isEmpty())
            m_ProfileCombo->setToolTip(error);
        }
        m_ProfileComboLoading = false;
        // A removed provider/model is repaired independently for every conversation. Existing
        // running requests keep the immutable PendingChat copy captured when Send was pressed.
        const bool repairedConnectionSelections = repairConversationConnectionSelections();
        if(conversation())
          syncConnectionSelectionFromConversation();
        else
          refreshModelCombo();
        if(repairedConnectionSelections && !m_LoadingConversations)
          scheduleConversationSave();
        refreshConnectionStatus();
        const QVector<ProfileRefreshCompletion> completions = std::move(m_ProfileRefreshCompletions);
        m_ProfileRefreshCompletions.clear();
        for(const ProfileRefreshCompletion &pendingCompletion : completions)
          if(pendingCompletion.owner && pendingCompletion.function)
            pendingCompletion.function();
      });
}

void AIAssistant::refreshModelCombo()
{
  if(!m_ModelCombo)
    return;
  QSignalBlocker blocker(m_ModelCombo);
  m_ModelCombo->clear();
  const QStringList models = m_ProfileModels.value(m_ActiveProfile);
  for(const QString &model : models)
    m_ModelCombo->addItem(model, model);
  if(m_ModelCombo->count() == 0)
  {
    m_ModelCombo->addItem(tr("Provider default"), QString());
    m_ModelCombo->setEnabled(false);
    m_ActiveModel.clear();
    return;
  }
  m_ModelCombo->setEnabled(true);
  int index = m_ModelCombo->findData(m_ActiveModel);
  if(index < 0)
    index = m_ModelCombo->findData(m_ProfileDefaultModels.value(m_ActiveProfile));
  if(index < 0)
    index = 0;
  m_ModelCombo->setCurrentIndex(index);
  m_ActiveModel = m_ModelCombo->currentData().toString();
}

bool AIAssistant::repairConversationConnectionSelections()
{
  if(m_ProfileComboLoading)
    return false;
  bool changed = false;
  const QString fallbackProfile = m_ActiveProfile;
  for(ConversationThread &thread : m_Conversations)
  {
    QString profile = thread.profileId;
    if(m_ProfileCombo->findData(profile) < 0)
      profile = fallbackProfile;

    QString model = thread.modelId;
    const QStringList models = m_ProfileModels.value(profile);
    if(models.isEmpty())
    {
      model.clear();
    }
    else if(!models.contains(model))
    {
      model = m_ProfileDefaultModels.value(profile);
      if(!models.contains(model))
        model = models.front();
    }

    if(thread.profileId != profile || thread.modelId != model)
    {
      thread.profileId = profile;
      thread.modelId = model;
      changed = true;
    }
  }
  return changed;
}

void AIAssistant::syncConnectionSelectionFromConversation()
{
  if(m_ProfileComboLoading || !m_ProfileCombo || !m_ModelCombo)
    return;
  ConversationThread *thread = conversation();
  if(!thread)
    return;

  int profileIndex = m_ProfileCombo->findData(thread->profileId);
  if(profileIndex < 0 && !m_ActiveProfile.isEmpty())
    profileIndex = m_ProfileCombo->findData(m_ActiveProfile);
  if(profileIndex < 0 && !m_ProfileKeyEnvs.isEmpty())
    profileIndex = 0;

  QSignalBlocker profileBlocker(m_ProfileCombo);
  if(profileIndex < 0)
  {
    m_ActiveProfile.clear();
    m_ActiveKeyEnv.clear();
    m_ActiveModel.clear();
    thread->profileId.clear();
    thread->modelId.clear();
    refreshModelCombo();
    refreshConnectionStatus();
    return;
  }

  m_ProfileCombo->setCurrentIndex(profileIndex);
  m_ActiveProfile = m_ProfileCombo->itemData(profileIndex).toString();
  m_ActiveKeyEnv = m_ProfileCombo->itemData(profileIndex, Qt::UserRole + 1).toString();
  thread->profileId = m_ActiveProfile;
  const QStringList models = m_ProfileModels.value(m_ActiveProfile);
  m_ActiveModel = thread->modelId;
  if(models.isEmpty())
    m_ActiveModel.clear();
  else if(!models.contains(m_ActiveModel))
  {
    m_ActiveModel = m_ProfileDefaultModels.value(m_ActiveProfile);
    if(!models.contains(m_ActiveModel))
      m_ActiveModel = models.front();
  }
  refreshModelCombo();
  thread->modelId = m_ActiveModel;
  refreshConnectionStatus();
}

void AIAssistant::selectProfile(int index)
{
  if(m_ProfileComboLoading || index < 0)
    return;
  const QString profile = m_ProfileCombo->itemData(index).toString();
  if(profile.isEmpty() || profile == m_ActiveProfile)
    return;

  // The picker beside Send belongs to the selected conversation. A running turn keeps its
  // submit-time copy, so another conversation can choose a different provider immediately.
  m_ActiveProfile = profile;
  m_ActiveKeyEnv = m_ProfileCombo->itemData(index, Qt::UserRole + 1).toString();
  m_ActiveModel = m_ProfileDefaultModels.value(profile);
  refreshModelCombo();
  if(ConversationThread *thread = conversation())
  {
    thread->profileId = m_ActiveProfile;
    thread->modelId = m_ActiveModel;
    thread->updatedMs = QDateTime::currentMSecsSinceEpoch();
  }
  appendSystem(tr("Active connection: %1").arg(m_ProfileCombo->itemText(index)));
  scheduleConversationSave();
  refreshConnectionStatus();
}

void AIAssistant::selectModel(int index)
{
  if(m_ProfileComboLoading || index < 0 || !m_ModelCombo)
    return;
  const QString model = m_ModelCombo->itemData(index).toString();
  if(model == m_ActiveModel)
    return;
  m_ActiveModel = model;
  if(ConversationThread *thread = conversation())
  {
    thread->modelId = model;
    thread->updatedMs = QDateTime::currentMSecsSinceEpoch();
  }
  appendSystem(model.isEmpty() ? tr("Using the provider's default model.")
                               : tr("Model for new tasks: %1").arg(model));
  scheduleConversationSave();
  refreshConnectionStatus();
}

bool AIAssistant::connectionMutationAllowed(QWidget *parent) const
{
  if(m_RunningTasks.isEmpty())
    return true;

  QMessageBox::warning(parent, tr("AI Connections"),
                       tr("Wait for active AI tasks before changing connections."));
  return false;
}

QWidget *AIAssistant::CreateSettingsPage(QWidget *parent)
{
  QWidget *page = new QWidget(parent);
  const std::shared_ptr<AIHostCommandRunner> profileCommandRunner =
      std::make_shared<AIHostCommandRunner>();
  const std::shared_ptr<AIProviderCatalogState> providerCatalog =
      std::make_shared<AIProviderCatalogState>();
  for(const QJsonValue &value : AIConfiguration::providerPresets())
    if(supportedQRenderDocConnection(value.toObject()))
      providerCatalog->presets.append(value);
  // The normal MainWindow Settings entry does not pass through manageConnections(). Mark the
  // shared configuration surface busy for its whole lifetime so profile edits and subscription
  // setup cannot race active task state.
  m_ConnectionDialogOpen = true;
  connect(page, &QObject::destroyed, this, [this]() {
    m_ConnectionDialogOpen = false;
    if(!m_ShuttingDown)
      refreshConnectionStatus();
  });
  QVBoxLayout *root = new QVBoxLayout(page);
  root->setContentsMargins(8, 8, 8, 8);

  QLabel *intro = new QLabel(
      tr("Add model APIs or Agent subscriptions here. API connections use the same RenderDoc "
         "deepagents framework maintained on GitHub. Managed subscriptions run the vendor's "
         "native Agent. Both can use Bread Skills and task-scoped Host tools. "
         "API keys can be stored securely in Windows Credential Manager "
         "or kept only for the current qrenderdoc session."),
      page);
  intro->setWordWrap(true);
  root->addWidget(intro);

  QHBoxLayout *body = new QHBoxLayout();
  QListWidget *connections = new QListWidget(page);
  connections->setObjectName(lit("AIConnectionList"));
  connections->setMinimumWidth(250);
  body->addWidget(connections, 1);

  QVBoxLayout *actions = new QVBoxLayout();
  QPushButton *add = new QPushButton(tr("Add..."), page);
  QPushButton *edit = new QPushButton(tr("Edit..."), page);
  QPushButton *remove = new QPushButton(tr("Remove"), page);
  QPushButton *makeDefault = new QPushButton(tr("Set as Default"), page);
  QPushButton *check = new QPushButton(tr("Check Connection"), page);
  check->setToolTip(tr("Send a small test prompt and verify an actual MCP tool call. This uses the selected connection's model quota."));
  add->setEnabled(false);
  edit->setEnabled(false);
  actions->addWidget(add);
  actions->addWidget(edit);
  actions->addWidget(remove);
  actions->addSpacing(8);
  actions->addWidget(makeDefault);
  actions->addWidget(check);
  actions->addStretch(1);
  body->addLayout(actions);

  QGroupBox *detailsBox = new QGroupBox(tr("Connection details"), page);
  QVBoxLayout *detailsLayout = new QVBoxLayout(detailsBox);
  QTextBrowser *details = new QTextBrowser(detailsBox);
  details->setText(tr("Select a connection, or add one to get started."));
  details->setTextInteractionFlags(Qt::TextSelectableByMouse | Qt::TextSelectableByKeyboard);
  details->setOpenExternalLinks(false);
  details->setOpenLinks(false);
  details->setFrameShape(QFrame::NoFrame);
  details->setSizeAdjustPolicy(QAbstractScrollArea::AdjustIgnored);
  details->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
  details->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
  detailsLayout->addWidget(details, 1);
  body->addWidget(detailsBox, 2);

  // Connections, Skills, MCP servers and unpackers are four kinds of the same thing --
  // something Bread AI can call -- so they are four tabs rather than one page with
  // the connection list on top and everything else crowded underneath it.
  QWidget *connectionPage = new QWidget(page);
  connectionPage->setLayout(body);
  root->addWidget(createToolCatalogSection(page, connectionPage), 1);

  QLabel *footer = new QLabel(
      tr("The connection name is for display. qrenderdoc creates an internal stable identifier "
         "automatically. Models enabled here appear beside Send in the Bread AI panel. The "
         "first connection becomes the default automatically. Use Fetch Models for an API check, "
         "or Edit then Connect/Test for a subscription check."),
      page);
  footer->setWordWrap(true);
  root->addWidget(footer);

  auto profileById = [this](const QString &id) {
    for(const QJsonValue &value : m_ProfileRows)
      if(value.toObject().value(lit("id")).toString() == id)
        return value.toObject();
    return QJsonObject();
  };

  const std::shared_ptr<bool> pageCommandRunning = std::make_shared<bool>(false);
  std::function<void()> updateSelection = [=]() {
    const bool selected = connections->currentItem() != NULL;
    const bool interactive = !*pageCommandRunning;
    connections->setEnabled(interactive);
    add->setEnabled(interactive);
    edit->setEnabled(interactive && selected);
    remove->setEnabled(interactive && selected);
    makeDefault->setEnabled(interactive && selected);
    check->setEnabled(interactive && selected);
    if(!selected)
    {
      details->setText(tr("Select a connection, or add one to get started."));
      return;
    }
    const QJsonObject row = profileById(connections->currentItem()->data(Qt::UserRole).toString());
    const bool api = row.value(lit("kind")).toString() == lit("model");
    const QStringList models = m_ProfileModels.value(row.value(lit("id")).toString());
    QString text =
        tr("<b>%1</b><br>Type: %2<br>Provider: %3<br>Endpoint: %4<br>"
           "Default model: %5<br>Enabled models: %6")
            .arg(connectionDisplayName(row).toHtmlEscaped(),
                 api ? tr("Model API") : tr("Agent subscription"),
                 row.value(lit("preset")).toString().toHtmlEscaped(),
                 row.value(lit("endpoint")).toString().isEmpty()
                     ? tr("Managed by provider")
                     : row.value(lit("endpoint")).toString().toHtmlEscaped(),
                 m_ProfileDefaultModels.value(row.value(lit("id")).toString()).isEmpty()
                     ? tr("Provider default")
                     : m_ProfileDefaultModels.value(row.value(lit("id")).toString()).toHtmlEscaped(),    // i18n-ignore: stable ReplayLab profile-id JSON key
                 models.isEmpty() ? tr("Provider default") : models.join(lit(", ")).toHtmlEscaped());
    if(row.value(lit("active")).toBool())
      text += tr("<br><b>Default connection</b>");
    details->setText(text);
  };

  const std::function<void(bool)> setPageCommandRunning =
      [pageCommandRunning, updateSelection](bool running) {
        *pageCommandRunning = running;
        updateSelection();
      };

  std::function<void(const QString &)> reloadList = [=](const QString &preferred) {
    connections->clear();
    int preferredRow = -1;
    int activeRow = -1;
    for(const QJsonValue &value : m_ProfileRows)
    {
      const QJsonObject row = value.toObject();
      const QString id = row.value(lit("id")).toString();
      const bool api = row.value(lit("kind")).toString() == lit("model");
      QString label = connectionDisplayName(row);
      label += api ? tr("  -  API") : tr("  -  Subscription");
      if(row.value(lit("active")).toBool())
        label.prepend(lit("* "));
      QListWidgetItem *item = new QListWidgetItem(label, connections);
      item->setData(Qt::UserRole, id);
      if(id == preferred)
        preferredRow = connections->count() - 1;
      if(row.value(lit("active")).toBool())
        activeRow = connections->count() - 1;
    }
    const int row = preferredRow >= 0 ? preferredRow : activeRow;
    if(row >= 0)
      connections->setCurrentRow(row);
    else if(connections->count() > 0)
      connections->setCurrentRow(0);
    updateSelection();
  };
  const std::function<void(const QString &)> refreshPageProfiles =
      [this, page, reloadList, setPageCommandRunning](const QString &preferred) {
        setPageCommandRunning(true);
        refreshProfiles(page, [reloadList, setPageCommandRunning, preferred]() {
          reloadList(preferred);
          setPageCommandRunning(false);
        });
      };

  connect(connections, &QListWidget::itemSelectionChanged, page, updateSelection);

  std::function<void(const QJsonObject &)> editConnection =
      [this, page, refreshPageProfiles, profileCommandRunner,
       providerCatalog](const QJsonObject &existing) {
        const QJsonArray presets = providerCatalog->presets;
        const bool editing = !existing.isEmpty();
        QString existingId = existing.value(lit("id")).toString();
        bool profileExists = editing;
        bool profileSavedDuringDialog = false;

        QDialog dialog(page);
        dialog.setWindowTitle(editing ? tr("Edit AI Connection") : tr("Add AI Connection"));
        dialog.resize(680, 650);
        QVBoxLayout *dialogRoot = new QVBoxLayout(&dialog);
        QFormLayout *general = new QFormLayout();
        QLineEdit *name = new QLineEdit(&dialog);
        name->setPlaceholderText(tr("For example: Work OpenAI or Personal Codex"));
        QComboBox *mode = new QComboBox(&dialog);
        mode->addItem(tr("Model API"), lit("model"));
        mode->addItem(tr("Agent Subscription (ACP / app-server)"), lit("agent"));
        QCheckBox *defaultConnection =
            new QCheckBox(tr("Make default after saving"), &dialog);
        general->addRow(tr("Connection name"), name);
        general->addRow(tr("Connection type"), mode);
        general->addRow(QString(), defaultConnection);
        dialogRoot->addLayout(general);

        QStackedWidget *modePages = new QStackedWidget(&dialog);
        QWidget *apiPage = new QWidget(modePages);
        QFormLayout *apiForm = new QFormLayout(apiPage);
        QComboBox *apiProvider = new QComboBox(apiPage);
        QLineEdit *baseUrl = new QLineEdit(apiPage);
        baseUrl->setPlaceholderText(tr("Provider API base URL"));
        QLineEdit *apiKey = new QLineEdit(apiPage);
        apiKey->setEchoMode(QLineEdit::Password);
        apiKey->setPlaceholderText(editing ? tr("Leave blank to keep the existing credential")
                                           : tr("Enter the provider API key"));
        QCheckBox *rememberKey = new QCheckBox(
            tr("Securely remember this key in Windows Credential Manager"), apiPage);
        rememberKey->setChecked(true);
        QLabel *credentialStatus = new QLabel(apiPage);
        credentialStatus->setWordWrap(true);
        credentialStatus->setTextInteractionFlags(Qt::TextSelectableByMouse);
        setDiagnosticStatus(
            credentialStatus, DiagnosticLevel::Info,
            editing ? tr("Checking for a saved credential...")
                    : tr("No saved credential yet. Enter a key to save it securely."));
        QPushButton *forgetCredential = new QPushButton(tr("Forget Saved Key"), apiPage);
        forgetCredential->setEnabled(false);
        QHBoxLayout *credentialStatusLayout = new QHBoxLayout();
        credentialStatusLayout->setContentsMargins(0, 0, 0, 0);
        credentialStatusLayout->addWidget(credentialStatus, 1);
        credentialStatusLayout->addWidget(forgetCredential);
        QWidget *credentialStatusWidget = new QWidget(apiPage);
        credentialStatusWidget->setLayout(credentialStatusLayout);
        QLineEdit *azureVersion = new QLineEdit(apiPage);
        azureVersion->setPlaceholderText(tr("Required only by legacy Azure deployment URLs"));
        QLineEdit *vertexProject = new QLineEdit(apiPage);
        vertexProject->setPlaceholderText(tr("Google Cloud project ID"));
        QLineEdit *vertexLocation = new QLineEdit(apiPage);
        vertexLocation->setPlaceholderText(tr("For example: us-central1 or global"));
        QLabel *apiHint = new QLabel(apiPage);
        apiHint->setWordWrap(true);
        apiForm->addRow(tr("Provider"), apiProvider);
        apiForm->addRow(tr("API address"), baseUrl);
        apiForm->addRow(tr("API key"), apiKey);
        apiForm->addRow(QString(), rememberKey);
        apiForm->addRow(tr("Saved key"), credentialStatusWidget);
        apiForm->addRow(tr("Azure API version"), azureVersion);
        apiForm->addRow(tr("Vertex project"), vertexProject);
        apiForm->addRow(tr("Vertex location"), vertexLocation);
        apiForm->addRow(QString(), apiHint);
        modePages->addWidget(apiPage);

        QWidget *agentPage = new QWidget(modePages);
        QFormLayout *agentForm = new QFormLayout(agentPage);
        QComboBox *agentProvider = new QComboBox(agentPage);
        QPlainTextEdit *agentCommand = new QPlainTextEdit(agentPage);
        agentCommand->setMaximumHeight(72);
        agentCommand->setPlaceholderText(
            tr("Custom ACP only: one non-secret executable or argument per line"));
        QTextBrowser *authStatus = new QTextBrowser(agentPage);
        authStatus->setTextInteractionFlags(Qt::TextBrowserInteraction |
                                            Qt::TextSelectableByKeyboard);
        authStatus->setOpenExternalLinks(false);
        authStatus->setOpenLinks(false);
        authStatus->setFrameShape(QFrame::NoFrame);
        authStatus->setMinimumHeight(58);
        authStatus->setMaximumHeight(110);
        setDiagnosticStatus(
            authStatus, DiagnosticLevel::Info,
            tr("Save this connection, then reopen it here to sign in with the provider."));
        QHBoxLayout *loginButtons = new QHBoxLayout();
        QComboBox *authFlow = new QComboBox(agentPage);
        authFlow->addItem(tr("Browser sign-in"), lit("browser"));
        authFlow->addItem(tr("Device code"), lit("device"));
        QPushButton *connectSubscription =
            new QPushButton(tr("Connect / Sign in and Test"), agentPage);
        QPushButton *cancelSubscription = new QPushButton(tr("Cancel"), agentPage);
        cancelSubscription->setVisible(false);
        loginButtons->addWidget(authFlow);
        loginButtons->addWidget(connectSubscription);
        loginButtons->addWidget(cancelSubscription);
        loginButtons->addStretch(1);
        QWidget *loginWidget = new QWidget(agentPage);
        loginWidget->setLayout(loginButtons);
        QCheckBox *trustACP = new QCheckBox(
            tr("I trust this custom ACP command to run as a local process"), agentPage);
        QLabel *agentHint = new QLabel(agentPage);
        agentHint->setWordWrap(true);
        agentForm->addRow(tr("Provider"), agentProvider);
        agentForm->addRow(tr("Agent command"), agentCommand);
        agentForm->addRow(tr("Process trust"), trustACP);
        agentForm->addRow(tr("Authentication"), authStatus);
        agentForm->addRow(QString(), loginWidget);
        agentForm->addRow(QString(), agentHint);
        modePages->addWidget(agentPage);
        dialogRoot->addWidget(modePages);

        QGroupBox *modelsBox = new QGroupBox(tr("Models available in the chat selector"), &dialog);
        QVBoxLayout *modelsRoot = new QVBoxLayout(modelsBox);
        QLabel *modelHint = new QLabel(
            tr("Fetch the provider's model list, then enable only the models you want to switch "
               "between. You can also add an exact model or deployment name manually."),
            modelsBox);
        modelHint->setWordWrap(true);
        modelsRoot->addWidget(modelHint);
        QListWidget *models = new QListWidget(modelsBox);
        models->setMinimumHeight(120);
        modelsRoot->addWidget(models, 1);
        QHBoxLayout *modelActions = new QHBoxLayout();
        QPushButton *fetchModels = new QPushButton(tr("Fetch Models"), modelsBox);
        QLineEdit *customModel = new QLineEdit(modelsBox);
        customModel->setPlaceholderText(tr("Exact model or deployment name"));
        QPushButton *addModel = new QPushButton(tr("Add"), modelsBox);
        // Optional, per-preset button to verify a model end to end with one short prompt.
        // Hidden by default; shown only for presets that support it (e.g. Claude Code).
        QPushButton *testModel = new QPushButton(tr("Test Model"), modelsBox);
        testModel->setToolTip(
            tr("Send one short prompt to the selected model to confirm the whole link works. "
               "This uses a small amount of your subscription quota."));
        testModel->setVisible(false);
        modelActions->addWidget(fetchModels);
        modelActions->addWidget(customModel, 1);
        modelActions->addWidget(addModel);
        modelActions->addWidget(testModel);
        modelsRoot->addLayout(modelActions);
        QFormLayout *defaultModelForm = new QFormLayout();
        QComboBox *defaultModel = new QComboBox(modelsBox);
        defaultModelForm->addRow(tr("Default model"), defaultModel);
        modelsRoot->addLayout(defaultModelForm);
        dialogRoot->addWidget(modelsBox, 1);

        QDialogButtonBox *buttons =
            new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel, &dialog);
        buttons->button(QDialogButtonBox::Save)->setText(tr("Save"));
        buttons->button(QDialogButtonBox::Cancel)->setText(tr("Cancel"));
        dialogRoot->addWidget(buttons);
        connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);

        auto presetObject = [&presets](const QString &id) {
          for(const QJsonValue &value : presets)
            if(value.toObject().value(lit("id")).toString() == id)
              return value.toObject();
          return QJsonObject();
        };
        auto populateProviders = [&]() {
          const QString kind = mode->currentData().toString();
          QComboBox *combo = kind == lit("model") ? apiProvider : agentProvider;
          const QString wanted = existing.value(lit("preset")).toString();
          QSignalBlocker blocker(combo);
          combo->clear();
          int selected = -1;
          for(const QJsonValue &value : presets)
          {
            const QJsonObject preset = value.toObject();
            if(preset.value(lit("kind")).toString() != kind)
              continue;
            combo->addItem(localizeProviderCatalogField(preset, "title"),
                           preset.value(lit("id")).toString());
            if(preset.value(lit("id")).toString() == wanted)
              selected = combo->count() - 1;
          }
          if(selected >= 0)
            combo->setCurrentIndex(selected);
          modePages->setCurrentIndex(kind == lit("model") ? 0 : 1);
        };
        auto selectedPresetId = [&]() {
          return mode->currentData().toString() == lit("model")
                     ? apiProvider->currentData().toString()
                     : agentProvider->currentData().toString();
        };
        auto selectedProviderMatchesEditingProfile = [&]() {
          // i18n-ignore: ReplayLab profile JSON field used only for identity comparison.
          return !editing || existing.value(lit("preset")).toString() == selectedPresetId();
        };
        QString lastPresetId;
        bool hasStoredCredential = false;
        bool credentialStoreSupported = true;
        bool subscriptionAuthenticated = false;
        int credentialStatusGeneration = 0;
        int authStatusGeneration = 0;
        bool rememberKeyTouched = false;
        bool dialogCommandRunning = false;
        typedef QVector<QPair<QPointer<QWidget>, bool>> DialogControlStates;
        auto beginDialogCommand = [&](bool supersedeCredentialProbe = true,
                                      bool supersedeAuthProbe = true) {
          DialogControlStates states;
          dialogCommandRunning = true;
          // Any in-flight read-only status result predates this explicit command and must not
          // overwrite the command's result when it arrives later.
          if(supersedeCredentialProbe)
            ++credentialStatusGeneration;
          if(supersedeAuthProbe)
            ++authStatusGeneration;
          const QList<QWidget *> controls = {
              name, mode, defaultConnection, modePages, modelsBox,
              buttons->button(QDialogButtonBox::Save),
          };
          for(QWidget *control : controls)
          {
            if(!control)
              continue;
            states.push_back(qMakePair(QPointer<QWidget>(control), control->isEnabled()));
            control->setEnabled(false);
          }
          return states;
        };
        auto finishDialogCommand = [&](const DialogControlStates &states) {
          for(const QPair<QPointer<QWidget>, bool> &state : states)
            if(state.first)
              state.first->setEnabled(state.second);
          dialogCommandRunning = false;
        };
        connect(rememberKey, &QCheckBox::clicked, &dialog, [&](bool) { rememberKeyTouched = true; });
        auto refreshCredentialStatus = [&]() {
          const int requestGeneration = ++credentialStatusGeneration;
          const QString requestPreset = selectedPresetId();
          if(!editing)
          {
            hasStoredCredential = false;
            credentialStoreSupported = true;
            setDiagnosticStatus(credentialStatus, DiagnosticLevel::Warning,
                                tr("No saved credential yet. Enter a key to save it securely."));
            forgetCredential->setEnabled(false);
            return;
          }
          setDiagnosticStatus(credentialStatus, DiagnosticLevel::Info,
                              tr("Checking for a saved credential..."));
          forgetCredential->setEnabled(false);
          (*profileCommandRunner)(
              // i18n-ignore: Fixed AgentHost CLI command and option identifiers; never shown.
              {lit("profile"), lit("credential"), lit("status"), lit("--profile"), existingId,
               // i18n-ignore: Fixed AgentHost CLI command and option identifiers; never shown.
               lit("--json")},
              QByteArray(), 5000, &dialog,
              [&, requestGeneration, requestPreset](const AIHostCommandReply &reply) {
                if(requestGeneration != credentialStatusGeneration ||
                   requestPreset != selectedPresetId() || !selectedProviderMatchesEditingProfile())
                  return;
                const QJsonDocument result = QJsonDocument::fromJson(reply.standardOutput);
                if(reply.exitCode != 0 || !result.isObject())
                {
                  hasStoredCredential = false;
                  setDiagnosticStatus(credentialStatus, DiagnosticLevel::Warning,
                                      reply.diagnostic.isEmpty()
                                          ? tr("Saved credential status is unavailable.")
                                          : reply.diagnostic);
                  forgetCredential->setEnabled(false);
                  return;
                }
                const QJsonObject payload = result.object();
                const QString state = payload.value(lit("status")).toString();
                credentialStoreSupported = payload.value(lit("supported")).toBool(true);
                hasStoredCredential = payload.value(lit("stored")).toBool() ||
                                      state == lit("stored") || state == lit("available");
                if(!credentialStoreSupported)
                  setDiagnosticStatus(
                      credentialStatus, DiagnosticLevel::Warning,
                      tr("Secure credential storage is not available on this platform; the key "
                         "can be used for this session only."));
                else if(hasStoredCredential)
                  setDiagnosticStatus(
                      credentialStatus, DiagnosticLevel::Success,
                      tr("A key is securely saved in Windows Credential Manager. Leave the key "
                         "field empty to keep it, or enter a new key to replace it."));
                else if(m_SessionSecrets.contains(existingId))
                  setDiagnosticStatus(
                      credentialStatus, DiagnosticLevel::Warning,
                      tr("A session-only key is active. Save with secure remembering enabled to "
                         "move it into Windows Credential Manager."));
                else
                  setDiagnosticStatus(credentialStatus, DiagnosticLevel::Warning,
                                      tr("No key is saved in Windows Credential Manager."));
                forgetCredential->setEnabled(hasStoredCredential);
                rememberKey->setEnabled(credentialStoreSupported);
                if(!credentialStoreSupported)
                  rememberKey->setChecked(false);
                else if(!rememberKeyTouched && hasStoredCredential)
                  rememberKey->setChecked(true);
              });
        };
        typedef std::function<void(bool, const QString &)> CredentialDeleteCompletion;
        auto deleteStoredCredential = [&](const QString &profile,
                                          const CredentialDeleteCompletion &completion) {
          runCLIAsync(
              // i18n-ignore: Fixed AgentHost CLI command and option identifiers; never shown to users.
              {lit("profile"), lit("credential"), lit("delete"), lit("--profile"), profile,
                lit("--json")},    // i18n-ignore: Fixed AgentHost CLI option identifier.
              QByteArray(), 5000, &dialog,
              [completion](int exitCode, const QByteArray &, const QString &diagnostic, bool) {
                completion(exitCode == 0, diagnostic);
              });
        };
        connect(forgetCredential, &QPushButton::clicked, &dialog, [&]() {
          if(dialogCommandRunning || !editing || !connectionMutationAllowed(&dialog))
            return;
          if(QMessageBox::question(
                 &dialog, tr("Forget Saved API Key"),
                 tr("Remove this connection's key from Windows Credential Manager?")) !=
             QMessageBox::Yes)
            return;
          const DialogControlStates controls = beginDialogCommand();
          deleteStoredCredential(
              existingId, [&, controls](bool deleted, const QString &forgetError) {
                finishDialogCommand(controls);
                if(!deleted)
                {
                  QMessageBox::critical(
                      &dialog, tr("Could not forget API key"),
                      forgetError.isEmpty()
                          ? tr("Windows Credential Manager rejected the request.")
                          : forgetError);
                  return;
                }
                m_SessionSecrets.remove(existingId);
                hasStoredCredential = false;
                rememberKeyTouched = true;
                rememberKey->setChecked(false);
                forgetCredential->setEnabled(false);
                setDiagnosticStatus(credentialStatus, DiagnosticLevel::Warning,
                                    tr("No key is saved in Windows Credential Manager."));
                apiKey->clear();
              });
        });
        auto updateProvider = [&]() {
          const QString currentPresetId = selectedPresetId();
          const bool providerSelectionChanged =
              !lastPresetId.isEmpty() && lastPresetId != currentPresetId;
          if(providerSelectionChanged)
          {
            // A late result for the old provider must never restore its credential or
            // authentication state after the user has selected a replacement.
            ++credentialStatusGeneration;
            ++authStatusGeneration;
            // Model ids and typed credentials are provider-scoped. Never carry either across a
            // provider change merely because the stable connection id is retained.
            models->clear();
            defaultModel->clear();
            customModel->clear();
            apiKey->clear();
            hasStoredCredential = false;
            credentialStoreSupported = true;
            rememberKeyTouched = false;
            rememberKey->setEnabled(true);
            rememberKey->setChecked(true);
            setDiagnosticStatus(credentialStatus, DiagnosticLevel::Info,
                                tr("Save this connection before storing a provider credential."));
            forgetCredential->setEnabled(false);
            subscriptionAuthenticated = false;
          }
          lastPresetId = currentPresetId;
          const QJsonObject preset = presetObject(currentPresetId);
          if(mode->currentData().toString() == lit("model"))
          {
            connectSubscription->setEnabled(false);
            modelHint->setText(
                tr("Fetch the provider's model list, then enable only the models you want to "
                   "switch between. You can also add an exact model or deployment name manually."));
            customModel->setPlaceholderText(tr("Exact model or deployment name"));
            testModel->setVisible(false);
            if(!editing)
            {
              QPushButton *saveButton = buttons->button(QDialogButtonBox::Save);
              if(saveButton)
                saveButton->setText(tr("Save"));
            }
            fetchModels->setEnabled(true);
            if(!editing || providerSelectionChanged)
              baseUrl->setText(editing &&
                                       existing.value(lit("preset")).toString() == currentPresetId
                                   ? existing.value(lit("endpoint")).toString()
                                   : preset.value(lit("baseUrl")).toString());
            const bool usesAPIKey = preset.value(lit("auth")).toString() == lit("api-key");
            apiKey->setEnabled(usesAPIKey);
            rememberKey->setVisible(usesAPIKey);
            credentialStatusWidget->setVisible(usesAPIKey);
            const bool azure = selectedPresetId() == lit("azure-openai");
            const QString endpoint = baseUrl->text().trimmed().toLower();
            const bool azureV1 = endpoint.endsWith(lit("/v1")) ||
                                 endpoint.contains(lit("/openai/v1")) ||
                                 endpoint.contains(lit("services.ai.azure.com"));
            azureVersion->setVisible(azure && !azureV1);
            apiForm->labelForField(azureVersion)->setVisible(azure && !azureV1);
            if(azureV1)
              azureVersion->clear();
            const bool vertex = selectedPresetId() == lit("vertex");
            vertexProject->setVisible(vertex);
            vertexLocation->setVisible(vertex);
            apiForm->labelForField(vertexProject)->setVisible(vertex);
            apiForm->labelForField(vertexLocation)->setVisible(vertex);
            apiHint->setText(localizeProviderCatalogField(preset, "note"));
          }
          else
          {
            const bool savedProviderSelected = selectedProviderMatchesEditingProfile();
            connectSubscription->setEnabled(!editing || savedProviderSelected);
            const QJsonArray commandArray = preset.value(lit("command")).toArray();
            const bool custom = selectedPresetId() == lit("custom-acp");
            // The trust confirmation for a built-in preset applies to the exact non-editable
            // catalog command, so never display a stale legacy profile override as that command.
            // Custom ACP remains user-owned and must preserve its edited argv during hydration.
            if(!editing || providerSelectionChanged || !custom)
            {
              QStringList commandLines;
              if(custom && editing &&
                 existing.value(lit("preset")).toString() == currentPresetId)
              {
                for(const QJsonValue &part : existing.value(lit("command")).toArray())
                  commandLines << part.toString();
              }
              else
              {
                for(const QJsonValue &part : commandArray)
                  commandLines << part.toString();
              }
              agentCommand->setPlainText(commandLines.join(QLatin1Char('\n')));
            }
            const QString protocol = preset.value(lit("protocol")).toString();
            const QString loginMode = preset.value(lit("subscriptionLoginMode")).toString();
            const bool builtInACP = protocol == lit("acp") && !custom;
            agentCommand->setReadOnly(!custom);
            trustACP->setVisible(protocol == lit("acp"));
            if(protocol == lit("acp"))
            {
              const bool previouslyTrusted =
                  editing && existing.value(lit("preset")).toString() == selectedPresetId() &&
                  existing.value(lit("options"))
                      .toObject()
                      .value(lit("allow_unsandboxed_agent"))
                      .toBool();
              trustACP->setText(
                  builtInACP
                      ? tr("Run the official %1 command shown above (required)")
                            .arg(localizeProviderCatalogField(preset, "title"))
                      : tr("I trust this custom ACP command to run as a local process"));
              trustACP->setChecked(builtInACP || previouslyTrusted);
              trustACP->setToolTip(
                  builtInACP
                      ? tr("This confirmation applies only to the exact non-editable command from "
                           "the built-in provider catalog. The launched Agent remains "
                           "limited by the per-turn tools and workflow checks. You may "
                           "uncheck it to prevent the CLI from running.")
                      : tr("Custom commands are never trusted automatically. Review every "
                           "argument above before checking this box."));
            }
            loginWidget->setVisible(true);
            authFlow->setVisible(protocol == lit("codex-app-server") ||
                                 selectedPresetId() == lit("copilot-acp"));
            connectSubscription->setText(protocol == lit("codex-app-server")
                                             ? tr("Connect / Sign in and Test")
                                             : custom ? tr("Test Custom ACP")
                                                      : tr("Connect and Test"));
            connectSubscription->setToolTip(
                profileExists
                    ? tr("Start the provider's official authentication flow and then run a "
                         "no-prompt connection handshake.")
                    : tr("Save this connection and immediately start the provider's official "
                         "authentication and no-prompt handshake."));
            if(editing && !savedProviderSelected)
              connectSubscription->setToolTip(
                  tr("Save this provider change before connecting."));
            if(!editing)
            {
              QPushButton *saveButton = buttons->button(QDialogButtonBox::Save);
              if(saveButton)
                saveButton->setText(tr("Save and Connect"));
            }
            // Codex enumerates models from a running, saved app-server. A preset that ships a
            // curated static list (e.g. Claude Code, which has no model-list API) can be fetched
            // immediately from just its preset id, so gate those on supportsModelDiscovery.
            fetchModels->setEnabled(
                (editing && savedProviderSelected &&
                 // i18n-ignore: provider protocol identifier used only for control flow.
                 protocol == lit("codex-app-server")) ||
                // i18n-ignore: provider protocol identifier used only for control flow.
                (protocol == lit("acp") &&
                 // i18n-ignore: provider-catalog JSON field name used only for control flow.
                 preset.value(lit("supportsModelDiscovery")).toBool()));
            modelHint->setText(
                tr("Bread AI imports model choices exposed by the provider's ACP/app-server "
                   "handshake. If the Agent exposes no standard list, leave this empty to use "
                   "its configured default, or add an exact model only when that CLI supports "
                   "ACP model selection."));
            // i18n-ignore: fixed preset identifier used only for control flow.
            if(currentPresetId == lit("claude-code-acp"))
            {
              modelHint->setText(
                  tr("Claude Code has no model-list API, so Fetch Models loads its current model "
                     "ids. Pick one, or add your own below using the model name plus version -- "
                     "for example claude-sonnet-5 or claude-haiku-4-5-20251001. Leave empty to "
                     "follow your subscription's default. Spacing and capitalization are "
                     "normalized for you; a model your plan does not include is rejected by "
                     "Claude Code at run time."));
              customModel->setPlaceholderText(tr("For example: claude-sonnet-5"));
              testModel->setVisible(true);
            }
            else
            {
              customModel->setPlaceholderText(tr("Exact model or deployment name"));
              testModel->setVisible(false);
            }
            if(editing && !savedProviderSelected)
              setDiagnosticStatus(authStatus, DiagnosticLevel::Warning,
                                  tr("Save this provider change before connecting."));
            else if(custom)
              setDiagnosticStatus(
                  authStatus, DiagnosticLevel::Info,
                  tr("Custom ACP has no standardized login command. Bread AI will only test the "
                     "configured initialize/session handshake. This command is saved in the "
                     "profile, so never put a token or password in its arguments."));
            else if(loginMode == lit("captured"))
              setDiagnosticStatus(
                  authStatus, DiagnosticLevel::Info,
                  tr("Bread AI will run the official provider login, open its safe authorization "
                     "URL, show any device code, and test ACP automatically."));
            else if(loginMode == lit("terminal"))
              setDiagnosticStatus(
                  authStatus, DiagnosticLevel::Info,
                  tr("Bread AI will open the official provider CLI in a contained terminal, wait "
                     "for its interactive sign-in to finish, and then test ACP automatically."));
            agentHint->setText(localizeProviderCatalogField(preset, "note"));
          }
        };
        auto updateDefaultModels = [&]() {
          const QString previous = defaultModel->currentData().toString();
          QSignalBlocker blocker(defaultModel);
          defaultModel->clear();
          for(int i = 0; i < models->count(); ++i)
          {
            QListWidgetItem *item = models->item(i);
            if(item->checkState() == Qt::Checked)
              defaultModel->addItem(item->text(), item->text());
          }
          int index = defaultModel->findData(previous);
          if(index < 0 && selectedProviderMatchesEditingProfile())
            index = defaultModel->findData(
                existing.value(lit("defaultModel")).toString(existing.value(lit("model")).toString()));
          if(index < 0 && defaultModel->count() > 0)
            index = 0;
          defaultModel->setCurrentIndex(index);
        };
        auto addModelItem = [&](const QString &model, bool enabled) {
          const QString trimmed = model.trimmed();
          if(trimmed.isEmpty())
            return;
          for(int i = 0; i < models->count(); ++i)
          {
            if(models->item(i)->text() == trimmed)
            {
              if(enabled)
                models->item(i)->setCheckState(Qt::Checked);
              return;
            }
          }
          QListWidgetItem *item = new QListWidgetItem(trimmed, models);
          item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
          item->setCheckState(enabled ? Qt::Checked : Qt::Unchecked);
        };

        connect(mode, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged),
                &dialog, [&](int) {
                  populateProviders();
                  updateProvider();
                  fetchModels->setText(tr("Fetch Models"));
                });
        connect(apiProvider, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged),
                &dialog, [&](int) {
                  updateProvider();
                  if(editing && selectedProviderMatchesEditingProfile())
                    refreshCredentialStatus();
                });
        connect(baseUrl, &QLineEdit::textChanged, &dialog, [&](const QString &) {
          if(selectedPresetId() == lit("azure-openai"))
            updateProvider();
        });
        connect(agentCommand, &QPlainTextEdit::textChanged, &dialog, [&]() {
          // Trust is granted to the reviewed argv, not to the profile id. Any custom command
          // change requires a fresh, visible confirmation before it can be saved or launched.
          if(selectedPresetId() == lit("custom-acp"))
            trustACP->setChecked(false);
        });
        connect(models, &QListWidget::itemChanged, &dialog,
                [&](QListWidgetItem *) { updateDefaultModels(); });
        connect(addModel, &QPushButton::clicked, &dialog, [&]() {
          addModelItem(customModel->text(), true);
          customModel->clear();
          updateDefaultModels();
        });
        connect(customModel, &QLineEdit::returnPressed, addModel, &QPushButton::click);

        connect(testModel, &QPushButton::clicked, &dialog, [&]() {
          if(dialogCommandRunning)
            return;
          // Testing a model launches the vendor CLI and sends a prompt, so it needs a saved,
          // signed-in connection. The selected list item wins, then the manual box, then the
          // configured default.
          if(!(profileExists && selectedProviderMatchesEditingProfile()))
          {
            QMessageBox::information(
                &dialog, tr("Test Model"),
                tr("Save this connection and finish its sign-in first, then reopen it to test a "
                   "model."));
            return;
          }
          QString modelToTest;
          if(models->currentItem())
            modelToTest = models->currentItem()->text();
          if(modelToTest.isEmpty())
            modelToTest = customModel->text().trimmed();
          if(modelToTest.isEmpty())
            modelToTest = defaultModel->currentData().toString();
          const QString modelLabel = modelToTest.isEmpty() ? tr("the default model") : modelToTest;
          if(QMessageBox::question(
                 &dialog, tr("Test Model"),
                 tr("Send one short prompt to \"%1\" to confirm it works? This uses a small amount "
                    "of your subscription quota.")
                     .arg(modelLabel),
                 QMessageBox::Ok | QMessageBox::Cancel, QMessageBox::Ok) != QMessageBox::Ok)
            return;
          // i18n-ignore: ReplayLab agent CLI subcommand and flag tokens, not user-facing.
          QStringList arguments = {lit("profile"), lit("test-model"), lit("--profile"), existingId,
                                   // i18n-ignore: ReplayLab agent CLI flag tokens, not user-facing.
                                   lit("--model"),    modelToTest,      lit("--json")};
          const DialogControlStates controls = beginDialogCommand(false, false);
          testModel->setEnabled(false);
          testModel->setText(tr("Testing..."));
          runCLIAsync(
              arguments, QByteArray(), 120000, &dialog,
              [&, controls, modelToTest](int testExit, const QByteArray &output,
                                         const QString &testError, bool) {
                finishDialogCommand(controls);
                testModel->setEnabled(true);
                testModel->setText(tr("Test Model"));
                const QJsonObject obj = QJsonDocument::fromJson(output).object();
                // i18n-ignore: JSON result field name, not user-facing.
                const bool ok = obj.value(lit("ok")).toBool() && testExit == 0;
                // Build the user-facing result in the UI layer so it is localizable; the model id
                // is inserted verbatim (a protected token) and the backend's raw English text is
                // kept only as a diagnostic detail, never as the primary label.
                const QString shownModel =
                    modelToTest.isEmpty() ? tr("the default model") : modelToTest;
                // i18n-ignore: JSON result field name, not user-facing.
                QString diag = obj.value(lit("diagnostic")).toString();
                if(diag.isEmpty())
                  diag = testError;
                if(ok)
                  QMessageBox::information(
                      &dialog, tr("Test Model"),
                      tr("%1 responded successfully to a short test prompt. The link works.")
                          .arg(shownModel));
                else
                  QMessageBox::warning(&dialog, tr("Test Model"),
                                       diag.isEmpty()
                                           ? tr("The test of %1 failed.").arg(shownModel)
                                           : tr("The test of %1 failed:\n\n%2").arg(shownModel, diag));
              });
        });

        connect(fetchModels, &QPushButton::clicked, &dialog, [&]() {
          if(dialogCommandRunning)
            return;
          // i18n-ignore: Fixed AgentHost CLI command identifiers; never shown to users.
          QStringList arguments = {lit("profile"), lit("discover-models")};
          QByteArray protectedInput;
          QString credentialProfile;
          const bool useSavedProfile =
              profileExists && selectedProviderMatchesEditingProfile();
          if(useSavedProfile && apiKey->text().isEmpty())
          {
            arguments << lit("--profile") << existingId;
            credentialProfile = existingId;
          }
          else
          {
            arguments << lit("--preset") << selectedPresetId();
            if(mode->currentData().toString() == lit("model") &&
               !baseUrl->text().trimmed().isEmpty())
              arguments << lit("--base-url") << baseUrl->text().trimmed();
            if(!apiKey->text().isEmpty())
            {
              arguments << lit("--api-key-stdin");
              protectedInput = apiKey->text().toUtf8();
            }
          }
          arguments << lit("--json");
          // Model discovery does not mutate either credential or subscription state. Let an
          // already-running status probe finish so its "Checking" UI cannot be orphaned.
          const DialogControlStates controls = beginDialogCommand(false, false);
          fetchModels->setEnabled(false);
          fetchModels->setText(tr("Fetching..."));
          runCLIAsync(
              arguments, protectedInput, 25000, &dialog,
              [&, controls, useSavedProfile](int modelExit, const QByteArray &output,
                                              const QString &modelError, bool) {
                finishDialogCommand(controls);
                const QJsonDocument result = QJsonDocument::fromJson(output);
                const QJsonObject currentPreset = presetObject(selectedPresetId());
                fetchModels->setEnabled(
                    // i18n-ignore: provider kind identifier used only for control flow.
                    mode->currentData().toString() == lit("model") ||
                    (editing && selectedProviderMatchesEditingProfile() &&
                     // i18n-ignore: provider protocol field and identifier used only for control flow.
                     currentPreset.value(lit("protocol")).toString() ==
                         // i18n-ignore: Fixed provider protocol identifier used only for control flow.
                         lit("codex-app-server")) ||
                    // A curated-list preset (e.g. Claude Code) can always be re-fetched.
                    // i18n-ignore: provider protocol identifier used only for control flow.
                    (currentPreset.value(lit("protocol")).toString() == lit("acp") &&
                     // i18n-ignore: provider-catalog JSON field name used only for control flow.
                     currentPreset.value(lit("supportsModelDiscovery")).toBool()));
                fetchModels->setText(tr("Fetch Models"));
                if(modelExit != 0 || !result.isObject())
                {
                  QMessageBox::warning(
                      &dialog, tr("Model discovery"),
                      modelError.isEmpty()
                          ? tr("The provider did not return a model list. You can add an exact "
                               "model name manually.")
                          : modelError);
                  return;
                }
                const QJsonObject payload = result.object();
                const QStringList enabledModels =
                    useSavedProfile ? m_ProfileModels.value(existingId) : QStringList();
                QSet<QString> enabled;
                for(const QString &enabledModel : enabledModels)
                  enabled.insert(enabledModel);
                models->clear();
                for(const QJsonValue &value : payload.value(lit("models")).toArray())
                  addModelItem(value.toString(),
                               enabled.contains(value.toString()) ||
                                   (!useSavedProfile && models->count() == 0));
                if(mode->currentData().toString() == lit("model") &&
                   !payload.value(lit("endpoint")).toString().isEmpty())
                  baseUrl->setText(payload.value(lit("endpoint")).toString());
                updateDefaultModels();
                if(profileSavedDuringDialog && models->count() > 0)
                {
                  const QString discoveredDefault = defaultModel->currentData().toString();
                  QStringList modelArguments = {lit("profile"), lit("models"), existingId};
                  for(int i = 0; i < models->count(); ++i)
                    if(models->item(i)->checkState() == Qt::Checked)
                      modelArguments << lit("--allowed-model") << models->item(i)->text();
                  if(!discoveredDefault.isEmpty())
                    modelArguments << lit("--default-model") << discoveredDefault;
                  modelArguments << lit("--json");
                  const DialogControlStates persistControls = beginDialogCommand(false, false);
                  runCLIAsync(
                      modelArguments, QByteArray(), 7000, &dialog,
                      [&, persistControls](int persistExit, const QByteArray &,
                                           const QString &persistError, bool) {
                        finishDialogCommand(persistControls);
                        if(persistExit != 0)
                          setDiagnosticStatus(
                              authStatus, DiagnosticLevel::Warning,
                              persistError.isEmpty()
                                  ? tr("Connected, but the discovered model selection could not "
                                       "be saved.")
                                  : persistError);
                        else
                          refreshPageProfiles(existingId);
                      });
                }
              },
              credentialProfile);
        });

        QUrl currentAuthUrl;
        QString currentDeviceCode;
        std::shared_ptr<SubscriptionProcessState> activeAuthOperation;
        bool dialogClosing = false;
        connect(&dialog, &QDialog::finished, &dialog,
                [&](int) { dialogClosing = true; });
        connect(authStatus, &QTextBrowser::anchorClicked, &dialog, [&](const QUrl &url) {
          if(!safeProviderAuthUrl(url))
          {
            setDiagnosticStatus(authStatus, DiagnosticLevel::Error,
                                tr("The provider returned an unsafe authorization URL. It was not "
                                   "opened."));
            return;
          }
          if(!QDesktopServices::openUrl(url))
            setAuthLinkStatus(
                authStatus, DiagnosticLevel::Warning,
                tr("The system browser could not be opened automatically. Use the verified link "
                   "below to continue sign-in."),
                url, currentDeviceCode);
          else
            setAuthLinkStatus(
                authStatus, DiagnosticLevel::Info,
                tr("The verified sign-in link was opened in your browser. The link remains "
                   "available below if you need to copy it."),
                url, currentDeviceCode);
        });
        auto updateAuthStatus = [&]() {
          const int requestGeneration = ++authStatusGeneration;
          if(!profileExists)
            return;
          const QString requestPreset = selectedPresetId();
          setDiagnosticStatus(authStatus, DiagnosticLevel::Info,
                              tr("Checking subscription status..."));
          (*profileCommandRunner)(
              // i18n-ignore: Fixed AgentHost CLI protocol command and flags; never shown to users.
              {lit("profile"), lit("subscription"), lit("status"), lit("--profile"), existingId,
               lit("--json")},    // i18n-ignore: AgentHost CLI protocol command and flags
              QByteArray(), SubscriptionStatusTimeoutMs, &dialog,
              [&, requestGeneration, requestPreset](const AIHostCommandReply &reply) {
                if(requestGeneration != authStatusGeneration || requestPreset != selectedPresetId())
                  return;
                const QJsonDocument status = parseJSONOrLastJSONLRecord(reply.standardOutput);
                if(reply.exitCode == 0 && status.isObject())
                {
                  const QJsonObject value = status.object();
                  const QJsonObject data = value.value(lit("data")).toObject();
                  const QString state =
                      value.value(lit("status")).toString(data.value(lit("status")).toString());    // i18n-ignore: ReplayLab status JSON keys
                  // i18n-ignore: ReplayLab status JSON field names; known values are localized below.
                  const QString rawMessage =
                      value.value(lit("message"))    // i18n-ignore: ReplayLab runtime-message JSON key
                          .toString(
                              data.value(lit("message"))    // i18n-ignore: ReplayLab runtime-message JSON key
                                  .toString(value.value(lit("installHint"))    // i18n-ignore: ReplayLab install-hint JSON key
                                                .toString(data.value(lit("installHint")).toString())));    // i18n-ignore: ReplayLab install-hint JSON key
                  // i18n-ignore: Stable ReplayLab runtime-message protocol fields.
                  const QString messageId =
                      value.value(lit("messageId"))    // i18n-ignore: ReplayLab message-id JSON key
                          .toString(    // i18n-ignore: ReplayLab protocol key
                              data.value(lit("messageId"))
                                  .toString());    // i18n-ignore: ReplayLab protocol key
                  QJsonArray messageArgs =
                      value.value(lit("messageArgs")).toArray();    // i18n-ignore: ReplayLab protocol key
                  if(messageArgs.isEmpty())
                    messageArgs = data.value(lit("messageArgs"))    // i18n-ignore: ReplayLab message-argument JSON key
                                      .toArray();    // i18n-ignore: ReplayLab protocol key
                  const QString message = localizeProviderRuntimeMessage(requestPreset, messageId,
                                                                         messageArgs, rawMessage);
                  const bool installed = value.contains(lit("installed"))
                                             ? value.value(lit("installed")).toBool()
                                             : data.value(lit("installed")).toBool(true);
                  // Preserve a successful connection/test from this UI session unless the backend
                  // provides an explicit positive or negative authentication result.
                  if(!installed)
                    subscriptionAuthenticated = false;
                  else if(value.value(lit("authenticated")).isBool())
                    subscriptionAuthenticated = value.value(lit("authenticated")).toBool();
                  else if(data.value(lit("authenticated")).isBool())
                    subscriptionAuthenticated = data.value(lit("authenticated")).toBool();
                  else if(state == lit("authenticated") || state == lit("connected"))
                    subscriptionAuthenticated = true;
                  else if(state == lit("unauthenticated") || state == lit("disconnected"))
                    subscriptionAuthenticated = false;
                  QString summary;
                  if(!installed)
                    summary =
                        message.isEmpty()
                            ? tr("The official provider CLI is not installed. Use the provider's "
                                 "official installation instructions, then try again.")
                            : message;
                  else if(subscriptionAuthenticated)
                    summary = message.isEmpty() ? tr("Connected and authenticated") : message;
                  else
                    summary = message.isEmpty() ? (state.isEmpty() ? tr("Ready to connect") : state)
                                                : message;
                  QJsonObject account = value.value(lit("account")).toObject();
                  if(account.isEmpty())
                    account = data.value(lit("account")).toObject();
                  const QString accountName =
                      account.value(lit("email")).toString(account.value(lit("name")).toString());    // i18n-ignore: provider account JSON keys
                  if(!accountName.isEmpty())
                    summary += tr("\nAccount: %1").arg(accountName);
                  setDiagnosticStatus(authStatus,
                                      !installed                  ? DiagnosticLevel::Error
                                      : subscriptionAuthenticated ? DiagnosticLevel::Success
                                                                  : DiagnosticLevel::Warning,
                                      summary, state);
                  const QString protocol =
                      presetObject(requestPreset).value(lit("protocol")).toString();    // i18n-ignore: provider protocol identifier
                  connectSubscription->setText(
                      subscriptionAuthenticated             ? tr("Test Connection")
                      : protocol == lit("codex-app-server") ? tr("Connect / Sign in and Test")    // i18n-ignore: provider protocol identifier
                      : requestPreset == lit("custom-acp")  ? tr("Test Custom ACP")    // i18n-ignore: stable provider preset identifier
                                                            : tr("Connect and Test"));
                }
                else
                {
                  subscriptionAuthenticated = false;
                  setDiagnosticStatus(authStatus, DiagnosticLevel::Warning,
                                      reply.diagnostic.isEmpty()
                                          ? tr("Authentication status unavailable")
                                          : reply.diagnostic);
                }
              });
        };
        connect(agentProvider, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged),
                &dialog, [&](int) {
                  updateProvider();
                  if(editing && selectedProviderMatchesEditingProfile())
                    updateAuthStatus();
                });
        auto startSubscriptionOperation = [&](const QString &operation, const QString &flow) {
          if(dialogCommandRunning || !profileExists || activeAuthOperation ||
             !connectionMutationAllowed(&dialog))
            return;
          // Ignore any read-only status probe that was already in flight. The explicit operation
          // below owns the authentication UI until it finishes and starts a fresh status probe.
          ++authStatusGeneration;
          const std::shared_ptr<SubscriptionProcessState> state =
              std::make_shared<SubscriptionProcessState>();
          state->operation = operation;
          state->presetId = selectedPresetId();
          state->agentProviderEnabled = agentProvider->isEnabled();
          state->agentCommandEnabled = agentCommand->isEnabled();
          state->trustACPEnabled = trustACP->isEnabled();
          state->authFlowEnabled = authFlow->isEnabled();
          state->modelsBoxEnabled = modelsBox->isEnabled();
          QPushButton *saveButton = buttons->button(QDialogButtonBox::Save);
          state->saveButtonEnabled = saveButton && saveButton->isEnabled();
          QProcess *processObject = new QProcess(&dialog);
          state->process = processObject;
          activeAuthOperation = state;
          configureProcess(processObject, existingId, &state->processInformation);
          connectSubscription->setEnabled(false);
          agentProvider->setEnabled(false);
          agentCommand->setEnabled(false);
          trustACP->setEnabled(false);
          authFlow->setEnabled(false);
          modelsBox->setEnabled(false);
          if(saveButton)
            saveButton->setEnabled(false);
          cancelSubscription->setVisible(true);
          cancelSubscription->setEnabled(true);
          currentAuthUrl.clear();
          currentDeviceCode.clear();
          setDiagnosticStatus(authStatus, DiagnosticLevel::Info,
                              operation == lit("test") ? tr("Testing provider connection...")
                                                        : tr("Starting sign-in..."));

          const auto stopForFailure = [this, state, authStatus,
                                       cancelSubscription](const QString &detail) {
            if(state->failureDetail.isEmpty())
              state->failureDetail = detail;
            if(state->completionHandled)
              return;
            setDiagnosticStatus(authStatus, DiagnosticLevel::Error, detail);
            cancelSubscription->setEnabled(false);
            closeKillOnCloseJob(state->job);
            const QPointer<QProcess> process = state->process;
            if(process && process->state() != QProcess::NotRunning)
              process->kill();
          };

          const auto consumeOutput = [&, state, stopForFailure](const QByteArray &chunk) {
            if(chunk.isEmpty())
              return;
            if(chunk.size() > MaxProcessOutputBytes ||
               state->outputBytes > MaxProcessOutputBytes - chunk.size() ||
               chunk.size() > MaxProcessOutputBytes - state->tail.size())
            {
              stopForFailure(
                  tr("Provider setup returned too much data; the contained process was stopped."));
              return;
            }
            state->outputBytes += chunk.size();
            state->tail += chunk;
            for(;;)
            {
              const int newline = state->tail.indexOf('\n');
              if(newline < 0)
                break;
              if(newline > MaxEventLineBytes)
              {
                stopForFailure(
                    tr("Provider setup returned an oversized event; the contained process was "
                       "stopped."));
                return;
              }
              const QJsonDocument event = QJsonDocument::fromJson(state->tail.left(newline));
              state->tail.remove(0, newline + 1);
              if(!event.isObject())
                continue;
              const QJsonObject object = event.object();
              const QJsonObject data = object.value(lit("data")).toObject();
              const QString kind = object.value(lit("kind")).toString(
                  object.value(lit("event")).toString(object.value(lit("type")).toString()));
              const QString protocolStatus =
                  object.value(lit("status")).toString(data.value(lit("status")).toString());
              // i18n-ignore: ReplayLab event JSON field names; known values are localized below.
              const QString rawMessage = object.value(lit("message")).toString(
                  data.value(lit("message")).toString());
              // i18n-ignore: ReplayLab event JSON field name used only to select a translation.
              const QString eventPresetKey = lit("preset");
              const QString eventPreset = object.value(eventPresetKey).toString(
                  data.value(eventPresetKey).toString(state->presetId));
              // i18n-ignore: Stable ReplayLab runtime-message protocol fields.
              const QString messageId =
                  object.value(lit("messageId")).toString(    // i18n-ignore: ReplayLab protocol key
                      data.value(lit("messageId")).toString());    // i18n-ignore: ReplayLab protocol key
              QJsonArray messageArgs =
                  object.value(lit("messageArgs")).toArray();    // i18n-ignore: ReplayLab protocol key
              if(messageArgs.isEmpty())
                messageArgs =
                    data.value(lit("messageArgs")).toArray();    // i18n-ignore: ReplayLab protocol key
              const QString message = localizeProviderRuntimeMessage(
                  eventPreset, messageId, messageArgs, rawMessage);
              if(!message.isEmpty())
                setDiagnosticStatus(authStatus, DiagnosticLevel::Info, message);
              const QString url = object.value(lit("authUrl")).toString(
                  object.value(lit("url")).toString(
                      data.value(lit("authUrl")).toString(data.value(lit("url")).toString())));
              const QString code = object.value(lit("userCode")).toString(
                  object.value(lit("code")).toString(
                      data.value(lit("userCode")).toString(data.value(lit("code")).toString())));
              if(!url.isEmpty() || !code.isEmpty())
              {
                currentDeviceCode = code;
                if(!url.isEmpty())
                {
                  const QUrl authUrl(url);
                  if(safeProviderAuthUrl(authUrl))
                  {
                    currentAuthUrl = authUrl;
                    const bool opened = QDesktopServices::openUrl(authUrl);
                    setAuthLinkStatus(
                        authStatus,
                        opened ? DiagnosticLevel::Info : DiagnosticLevel::Warning,
                        opened
                            ? tr("Complete sign-in in your browser. The verified link remains "
                                 "available below if you need to open or copy it again.")
                            : tr("The system browser could not be opened. Open or copy the "
                                 "verified link below to continue sign-in."),
                        currentAuthUrl, currentDeviceCode);
                  }
                  else
                  {
                    currentAuthUrl.clear();
                    setDiagnosticStatus(
                        authStatus, DiagnosticLevel::Error,
                        code.isEmpty()
                            ? tr("The provider returned an unsafe authorization URL; it was not "
                                 "opened.")
                            : tr("The provider returned an unsafe authorization URL; it was not "
                                 "opened.\nDevice code: %1")
                                  .arg(code));
                  }
                }
                else
                  setAuthLinkStatus(authStatus, DiagnosticLevel::Info,
                                    tr("Enter the device code in the provider sign-in flow."),
                                    QUrl(), currentDeviceCode);
              }
              if(object.value(lit("interactiveTerminalRequired")).toBool() ||
                 data.value(lit("interactiveTerminalRequired")).toBool())
                setDiagnosticStatus(
                    authStatus, DiagnosticLevel::Info,
                    tr("The provider requires interactive setup. Bread AI is opening a clearly "
                       "named terminal inside the same contained process job."));
              if(object.value(lit("terminalOpened")).toBool() ||
                 data.value(lit("terminalOpened")).toBool() || kind == lit("terminalOpened"))
                setDiagnosticStatus(
                    authStatus, DiagnosticLevel::Info,
                    message.isEmpty()
                        ? tr("Interactive provider terminal opened. Complete the prompts there; "
                             "this window will wait and test the connection automatically.")
                        : message);

              QJsonArray discovered = object.value(lit("models")).toArray();
              if(discovered.isEmpty())
                discovered = data.value(lit("models")).toArray();
              if(!discovered.isEmpty())
              {
                QStringList boundedModels;
                QSet<QString> seenModels;
                for(const QJsonValue &modelValue : discovered)
                {
                  const QString model = modelValue.toString().trimmed();
                  if(model.isEmpty() || model.toUtf8().size() > MaxModelIdBytes ||
                     seenModels.contains(model))
                    continue;
                  seenModels.insert(model);
                  boundedModels << model;
                  if(boundedModels.count() >= MaxDiscoveredModels)
                    break;
                }
                state->discoveredModels = boundedModels;
                QSet<QString> enabled;
                for(const QString &enabledModel : m_ProfileModels.value(existingId))
                  enabled.insert(enabledModel);
                models->clear();
                for(const QString &model : boundedModels)
                  addModelItem(model, enabled.contains(model) || models->count() == 0);
                updateDefaultModels();
              }

              const QJsonValue authenticatedValue =
                  object.value(lit("authenticated")).isBool()
                      ? object.value(lit("authenticated"))
                      : data.value(lit("authenticated"));
              const QJsonValue testCompletedValue =
                  object.value(lit("testCompleted")).isBool()
                      ? object.value(lit("testCompleted"))
                      : data.value(lit("testCompleted"));
              const bool explicitlyAuthenticated =
                  authenticatedValue.isBool() && authenticatedValue.toBool();
              const bool explicitlyTested =
                  testCompletedValue.isBool() && testCompletedValue.toBool();
              if(kind == lit("testCompleted"))
              {
                state->testTerminalSucceeded = protocolStatus == lit("ok") &&
                                               explicitlyAuthenticated && explicitlyTested;
                if(state->operation == lit("test"))
                  state->operationTerminalSucceeded = state->testTerminalSucceeded;
                if(!state->testTerminalSucceeded && state->failureDetail.isEmpty())
                  state->failureDetail =
                      message.isEmpty() ? tr("The provider connection test did not succeed.")
                                        : message;
              }
              else if(kind == lit("subscriptionCompleted") &&
                      state->operation == lit("connect"))
              {
                state->operationTerminalSucceeded = protocolStatus == lit("connected") &&
                                                    explicitlyAuthenticated && explicitlyTested;
                if(!state->operationTerminalSucceeded && state->failureDetail.isEmpty())
                  state->failureDetail =
                      message.isEmpty() ? tr("Provider sign-in did not complete successfully.")
                                        : message;
              }

              if(kind == lit("loginCompleted") || kind == lit("connectCompleted"))
                setDiagnosticStatus(
                    authStatus, DiagnosticLevel::Info,
                    message.isEmpty() ? tr("Provider sign-in completed; testing access...")
                                      : message);
              else if(kind == lit("subscriptionCompleted") || kind == lit("testCompleted"))
              {
                const bool eventSucceeded =
                    kind == lit("testCompleted") ? state->testTerminalSucceeded
                                                 : state->operationTerminalSucceeded;
                setDiagnosticStatus(
                    authStatus,
                    eventSucceeded ? DiagnosticLevel::Info : DiagnosticLevel::Error,
                    message.isEmpty()
                        ? eventSucceeded
                              ? tr("Connection completed; verifying provider access...")
                              : tr("The provider did not report a successful connection test.")
                        : message,
                    protocolStatus);
              }
            }
            if(state->tail.size() > MaxEventLineBytes)
              stopForFailure(
                  tr("Provider setup returned an unterminated oversized event; the contained "
                     "process was stopped."));
          };

          const auto completeOperation =
              [&, state, consumeOutput](int exitCode, QProcess::ExitStatus exitStatus,
                                        const QString &startError) {
                if(state->completionHandled)
                  return;
                state->completionHandled = true;
                if(state->cancelTimer)
                  state->cancelTimer->stop();
                disconnect(state->cancelConnection);
                const QPointer<QProcess> process = state->process;
                QString standardError = startError;
                if(process)
                {
                  consumeOutput(process->readAllStandardOutput());
                  const QString processError = QString::fromUtf8(
                      process->readAllStandardError().left(MaxEventLineBytes)).trimmed();
                  if(standardError.isEmpty())
                    standardError = processError;
                  disconnect(process, NULL, &dialog, NULL);
                }
                closeKillOnCloseJob(state->job);
                if(activeAuthOperation == state)
                  activeAuthOperation.reset();
                state->process = NULL;
                if(process)
                  process->deleteLater();

                const bool normalExit = exitStatus == QProcess::NormalExit;
                const bool processSucceeded = normalExit && exitCode == 0;
                const bool protocolSucceeded = state->testTerminalSucceeded &&
                                               state->operationTerminalSucceeded &&
                                               state->failureDetail.isEmpty();
                const bool succeeded = processSucceeded && protocolSucceeded;
                subscriptionAuthenticated = succeeded;
                if(dialogClosing)
                  return;

                agentProvider->setEnabled(state->agentProviderEnabled);
                agentCommand->setEnabled(state->agentCommandEnabled);
                trustACP->setEnabled(state->trustACPEnabled);
                authFlow->setEnabled(state->authFlowEnabled);
                modelsBox->setEnabled(state->modelsBoxEnabled);
                QPushButton *dialogSaveButton = buttons->button(QDialogButtonBox::Save);
                if(dialogSaveButton)
                  dialogSaveButton->setEnabled(state->saveButtonEnabled);
                connectSubscription->setEnabled(true);
                cancelSubscription->setVisible(false);
                const auto exposeSavedProfileClose = [&]() {
                  if(!profileSavedDuringDialog)
                    return;
                  QPushButton *saveButton = buttons->button(QDialogButtonBox::Save);
                  if(saveButton)
                  {
                    saveButton->setText(tr("Save"));
                    saveButton->setEnabled(true);
                  }
                };
                if(!succeeded)
                {
                  QString detail = state->failureDetail;
                  if(detail.isEmpty())
                    detail = standardError;
                  if(detail.isEmpty() && !normalExit)
                    detail = tr("Provider setup terminated unexpectedly.");
                  if(detail.isEmpty() && exitCode != 0)
                    detail = tr("Provider setup failed with exit code %1.").arg(exitCode);
                  if(detail.isEmpty())
                    detail = state->operation == lit("connect")
                                 ? tr("Provider setup exited without both a successful "
                                      "testCompleted event and subscriptionCompleted event.")
                                 : tr("Provider test exited without a successful testCompleted "
                                      "event.");
                  setDiagnosticStatus(authStatus, DiagnosticLevel::Error, detail);
                  exposeSavedProfileClose();
                }
                else
                {
                  const auto finishSuccessfulOperation =
                      [&, exposeSavedProfileClose](bool preserveModelPersistenceWarning) {
                        if(preserveModelPersistenceWarning)
                        {
                          // Do not let a delayed read-only status probe replace the actionable
                          // warning.
                          ++authStatusGeneration;
                          connectSubscription->setText(tr("Test Connection"));
                        }
                        else
                        {
                          updateAuthStatus();
                        }
                        // Codex app-server exposes an explicit model list after authentication.
                        // ACP model choices are imported from the session configOptions handshake
                        // when the Agent exposes them; manual entry remains available for Agents
                        // that do not.
                        if(!profileSavedDuringDialog && fetchModels->isEnabled())
                          fetchModels->click();
                        exposeSavedProfileClose();
                      };
                  if(profileSavedDuringDialog && !state->discoveredModels.isEmpty())
                  {
                    QString selectedDefault = defaultModel->currentData().toString();
                    if(!state->discoveredModels.contains(selectedDefault))
                    {
                      const QString existingDefault = m_ProfileDefaultModels.value(existingId);
                      selectedDefault = state->discoveredModels.contains(existingDefault)
                                            ? existingDefault
                                            : state->discoveredModels.first();
                    }
                    QStringList modelArguments = {lit("profile"), lit("models"), existingId};
                    for(const QString &model : state->discoveredModels)
                      modelArguments << lit("--allowed-model") << model;
                    modelArguments << lit("--default-model") << selectedDefault << lit("--json");
                    const DialogControlStates persistControls = beginDialogCommand();
                    runCLIAsync(
                        modelArguments, QByteArray(), 7000, &dialog,
                        [&, persistControls, finishSuccessfulOperation](
                            int persistExit, const QByteArray &, const QString &persistError,
                            bool) {
                          finishDialogCommand(persistControls);
                          bool preserveModelPersistenceWarning = false;
                          if(persistExit != 0)
                          {
                            preserveModelPersistenceWarning = true;
                            const QString warning =
                                persistError.isEmpty()
                                    ? tr("The provider connected, but its discovered model list "
                                         "could not be saved. Reopen this connection to retry.")
                                    : tr("The provider connected, but its discovered model list "
                                         "could not be saved: %1")
                                          .arg(persistError);
                            setDiagnosticStatus(authStatus, DiagnosticLevel::Warning, warning);
                            QMessageBox::warning(&dialog, tr("Model list not saved"), warning);
                          }
                          else
                          {
                            refreshPageProfiles(existingId);
                          }
                          finishSuccessfulOperation(preserveModelPersistenceWarning);
                        });
                    return;
                  }
                  finishSuccessfulOperation(false);
                }
              };

          QTimer *cancelTimer = new QTimer(processObject);
          cancelTimer->setSingleShot(true);
          cancelTimer->setInterval(2500);
          state->cancelTimer = cancelTimer;
          const auto escalateCancellation =
              [this, state, completeOperation](const QString &detail) {
                if(state->completionHandled)
                  return;
                if(state->failureDetail.isEmpty())
                  state->failureDetail = detail;
                const QPointer<QProcess> process = state->process;
                // Closing the Job is the authoritative tree-wide cancellation. kill() also gives
                // QProcess an immediate local terminal transition, but neither path waits on GUI.
                closeKillOnCloseJob(state->job);
                if(process && process->state() != QProcess::NotRunning)
                  process->kill();
                completeOperation(-1, QProcess::CrashExit, detail);
              };
          const QString cancelTimeoutDiagnostic = tr("Command timed out");
          connect(cancelTimer, &QTimer::timeout, &dialog,
                  [escalateCancellation, cancelTimeoutDiagnostic]() {
                    escalateCancellation(cancelTimeoutDiagnostic);
                  });
          const QString cancelWriteDiagnostic = tr("Provider setup terminated unexpectedly.");
          state->cancelConnection = connect(
              cancelSubscription, &QPushButton::clicked, &dialog,
              [state, authStatus, cancelSubscription, escalateCancellation,
               cancelWriteDiagnostic]() {
                const QPointer<QProcess> process = state->process;
                if(!process || state->completionHandled)
                  return;
                cancelSubscription->setEnabled(false);
                setDiagnosticStatus(
                    authStatus, DiagnosticLevel::Info,
                    QCoreApplication::translate("AIAssistant", "Cancelling connection setup..."));
                static const QByteArray cancelRecord("cancel\n");
                if(process->write(cancelRecord) != cancelRecord.size())
                {
                  escalateCancellation(cancelWriteDiagnostic);
                  return;
                }
                if(state->cancelTimer)
                  state->cancelTimer->start();
              });

          connect(processObject, &QProcess::started, &dialog, [this, state, stopForFailure]() {
            const QPointer<QProcess> process = state->process;
            if(!process || state->completionHandled)
              return;
            QString containmentError;
            if(!attachKillOnCloseJob(process, state->job, &containmentError))
            {
              stopForFailure(containmentError);
              return;
            }
            // Subscription connect/test are start-gated: the Python host cannot start any vendor
            // process until qrenderdoc contains this exact host and explicitly releases the gate.
            if(process->write("start\n") != qint64(sizeof("start\n") - 1))
            {
              stopForFailure(tr("Could not release the contained sign-in process."));
              return;
            }
            if(!resumeSuspendedProcess(state->processInformation, &containmentError))
              stopForFailure(containmentError);
          });
          connect(processObject, &QProcess::readyReadStandardOutput, &dialog,
                  [state, consumeOutput]() {
                    const QPointer<QProcess> process = state->process;
                    if(process && !state->completionHandled)
                      consumeOutput(process->readAllStandardOutput());
                  });
          connect(processObject,
                  static_cast<void (QProcess::*)(int, QProcess::ExitStatus)>(&QProcess::finished),
                  &dialog, [completeOperation](int exitCode, QProcess::ExitStatus exitStatus) {
                    completeOperation(exitCode, exitStatus, QString());
                  });
          connect(processObject, &QProcess::errorOccurred, &dialog,
                  [state, completeOperation](QProcess::ProcessError processError) {
                    if(processError != QProcess::FailedToStart || state->completionHandled)
                      return;
                    const QPointer<QProcess> process = state->process;
                    completeOperation(-1, QProcess::CrashExit,
                                      process ? process->errorString()
                                              : QCoreApplication::translate(
                                                    "AIAssistant",
                                                    "Provider setup could not be started."));
                  });
          QStringList subscriptionArguments = {
              // i18n-ignore: Fixed AgentHost CLI protocol command and flags; never shown to users.
              lit("profile"), lit("subscription"), operation, lit("--profile"), existingId,
              // i18n-ignore: Fixed AgentHost CLI option identifiers; never shown to users.
              lit("--start-gated"), lit("--json")};
          if(operation == lit("connect"))
            subscriptionArguments << lit("--flow") << flow;
          processObject->start(m_CLIPath, subscriptionArguments, QIODevice::ReadWrite);
        };
        connect(connectSubscription, &QPushButton::clicked, &dialog,
                [&]() {
                  // A provider selected while editing is not authoritative until profile update
                  // succeeds. Never authenticate the old saved profile under the new UI label.
                  if(!selectedProviderMatchesEditingProfile())
                    return;
                  if(!profileExists)
                  {
                    QPushButton *saveButton = buttons->button(QDialogButtonBox::Save);
                    if(saveButton && saveButton->isEnabled())
                      saveButton->click();
                    return;
                  }
                  startSubscriptionOperation(subscriptionAuthenticated ? lit("test")
                                                                       : lit("connect"),
                                             authFlow->currentData().toString());
                });
        if(editing)
        {
          // Hydrate the complete selected profile as one transaction. In particular, do not let
          // currentIndexChanged/textChanged handlers interpret programmatic field restoration as
          // a user changing provider: those handlers intentionally replace provider-scoped
          // values with preset defaults.
          const QSignalBlocker modeBlocker(mode);
          const QSignalBlocker apiProviderBlocker(apiProvider);
          const QSignalBlocker agentProviderBlocker(agentProvider);
          const QSignalBlocker baseUrlBlocker(baseUrl);
          const QSignalBlocker commandBlocker(agentCommand);
          const QSignalBlocker modelsBlocker(models);
          const QSignalBlocker authFlowBlocker(authFlow);

          name->setText(connectionDisplayName(existing));
          QString kind = existing.value(lit("kind")).toString();
          if(kind.isEmpty())
            kind = presetObject(existing.value(lit("preset")).toString())
                       .value(lit("kind"))
                       .toString();
          const int kindIndex = mode->findData(kind);
          mode->setCurrentIndex(kindIndex >= 0 ? kindIndex : 0);
          mode->setEnabled(false);
          populateProviders();

          const bool active = existing.value(lit("active")).toBool();
          defaultConnection->setChecked(active);
          if(active)
          {
            defaultConnection->setEnabled(false);
            defaultConnection->setToolTip(
                tr("Choose Set as Default on another connection to change the default."));
          }

          QString endpoint = existing.value(lit("endpoint")).toString();
          if(endpoint.isEmpty())
            endpoint = existing.value(lit("baseUrl")).toString();
          baseUrl->setText(endpoint);
          const QJsonObject options = existing.value(lit("options")).toObject();
          azureVersion->setText(options.value(lit("api_version")).toString());
          vertexProject->setText(options.value(lit("project")).toString());
          vertexLocation->setText(options.value(lit("location")).toString());

          QStringList commandLines;
          for(const QJsonValue &part : existing.value(lit("command")).toArray())
            commandLines << part.toString();
          agentCommand->setPlainText(commandLines.join(QLatin1Char('\n')));

          QString savedAuthFlow = options.value(lit("subscription_auth_flow")).toString();
          if(savedAuthFlow.isEmpty())
            savedAuthFlow = lit("browser");
          const int authFlowIndex = authFlow->findData(savedAuthFlow);
          authFlow->setCurrentIndex(authFlowIndex >= 0 ? authFlowIndex : 0);

          for(const QJsonValue &modelValue : existing.value(lit("models")).toArray())
            addModelItem(modelValue.toString(), true);
          if(models->count() == 0)
          {
            const QString legacyModel = existing.value(lit("defaultModel"))
                                            .toString(existing.value(lit("model")).toString());
            addModelItem(legacyModel, true);
          }

          // Establish the restored preset as the baseline before enabling the interactive update
          // path. This makes the following refresh visibility-only instead of a provider change.
          lastPresetId = selectedPresetId();
          updateProvider();
          updateDefaultModels();
        }
        else
        {
          populateProviders();
          updateProvider();
          name->setFocus();
        }
        modePages->setCurrentIndex(mode->currentData().toString() == lit("model") ? 0 : 1);
        connectSubscription->setEnabled(mode->currentData().toString() == lit("agent"));
        if(editing && existing.value(lit("kind")).toString() == lit("agent"))
          updateAuthStatus();
        if(editing && existing.value(lit("kind")).toString() == lit("model") &&
           presetObject(existing.value(lit("preset")).toString())
                   .value(lit("auth"))
                   .toString() == lit("api-key"))
          refreshCredentialStatus();

        connect(buttons, &QDialogButtonBox::accepted, &dialog, [&]() {
          if(dialogCommandRunning || activeAuthOperation || !connectionMutationAllowed(&dialog))
            return;
          const QString displayName = name->text().trimmed();
          if(displayName.isEmpty())
          {
            QMessageBox::warning(&dialog, tr("AI Connection"),
                                 tr("Enter a connection name."));
            return;
          }
          const QString presetId = selectedPresetId();
          const QJsonObject preset = presetObject(presetId);
          if(presetId.isEmpty())
            return;
          if(presetId == lit("azure-openai") && azureVersion->isVisible() &&
             azureVersion->text().trimmed().isEmpty())
          {
            QMessageBox::warning(
                &dialog, tr("Azure OpenAI"),
                tr("Legacy Azure deployment URLs require an API version. Azure v1/Foundry URLs "
                   "do not use this field."));
            return;
          }
          if(mode->currentData().toString() == lit("agent") &&
             preset.value(lit("protocol")).toString() == lit("acp") && !trustACP->isChecked())
          {
            const bool customACP = presetId == lit("custom-acp");
            QMessageBox::warning(&dialog, tr("ACP trust required"),
                                 customACP
                                     ? tr("Review every custom command argument, then explicitly "
                                          "check the Process trust box before this local process "
                                          "can run.")
                                     : tr("The built-in official CLI was disabled in Process "
                                          "trust. Check that visible box to allow the exact "
                                          "non-editable catalog command, or cancel."));
            return;
          }
          QStringList enabledModels;
          for(int i = 0; i < models->count(); ++i)
            if(models->item(i)->checkState() == Qt::Checked)
              enabledModels << models->item(i)->text();
          const QString chosenDefault = defaultModel->currentData().toString();
          if(!chosenDefault.isEmpty() && !enabledModels.contains(chosenDefault))
            enabledModels << chosenDefault;
          if(mode->currentData().toString() == lit("model") && chosenDefault.isEmpty())
          {
            QMessageBox::warning(
                &dialog, tr("AI model required"),
                tr("Fetch models or add an exact model/deployment name, enable it, and choose a "
                   "default model."));
            return;
          }

          const QString id = profileExists ? existingId : internalConnectionId();
          const bool presetChanged =
              editing && existing.value(lit("preset")).toString() != presetId;
          const bool usesAPIKey =
              mode->currentData().toString() == lit("model") &&
              preset.value(lit("auth")).toString() == lit("api-key");
          if(presetChanged && usesAPIKey && apiKey->text().isEmpty())
          {
            QMessageBox::warning(
                &dialog, tr("API key required"),
                tr("Enter the key for the newly selected provider. Credentials are never reused "
                   "across providers."));
            return;
          }
          const bool clearingLegacyAzureVersion =
              editing && presetId == lit("azure-openai") && !azureVersion->isVisible() &&
              !existing.value(lit("options")).toObject().value(lit("api_version")).toString().isEmpty(); // i18n-ignore: provider configuration JSON keys.
          QStringList arguments = {lit("profile"), profileExists ? lit("update") : lit("add"), id,
                                   lit("--preset"), presetId, lit("--title"), displayName};
          if(presetChanged || clearingLegacyAzureVersion)
          {
            // Rebuild every provider-scoped field from this dialog. Incremental update would keep
            // stale command/options/models/key metadata when changing provider or endpoint style.
            arguments << lit("--replace-provider");
          }
          if(mode->currentData().toString() == lit("model"))
          {
            if(!baseUrl->text().trimmed().isEmpty())
              arguments << lit("--base-url") << baseUrl->text().trimmed();
            const QString keyEnvironment = preset.value(lit("apiKeyEnv")).toString();
            if(!keyEnvironment.isEmpty())
              arguments << lit("--api-key-env") << keyEnvironment;
            arguments << lit("--capability") << lit("tools");
            if(presetId == lit("azure-openai") && azureVersion->isVisible() &&
               !azureVersion->text().trimmed().isEmpty())
              arguments << lit("--option")
                        << (lit("api_version=") + azureVersion->text().trimmed());
            if(presetId == lit("vertex"))
            {
              if(!vertexProject->text().trimmed().isEmpty())
                arguments << lit("--option")
                          << (lit("project=") + vertexProject->text().trimmed());
              if(!vertexLocation->text().trimmed().isEmpty())
                arguments << lit("--option")
                          << (lit("location=") + vertexLocation->text().trimmed());
            }
          }
          else
          {
            const QString protocol = preset.value(lit("protocol")).toString();
            // i18n-ignore: stable provider preset identifier used only for control flow.
            const bool customACP = presetId == lit("custom-acp");
            if(!customACP)
            {
              // Built-in Agent commands are catalog-owned. Clear any legacy profile override so
              // the immutable command shown above is also the exact command resolved at runtime.
              // i18n-ignore: internal CLI flag and empty JSON argv; never shown as UI text.
              arguments << lit("--command-json") << lit("[]");
            }
            const bool supportsAuthFlow =
                protocol == lit("codex-app-server") || presetId == lit("copilot-acp");
            if(supportsAuthFlow)
              arguments << lit("--option")
                        << (lit("subscription_auth_flow=") +
                            authFlow->currentData().toString());
            if(protocol == lit("acp"))
            {
              arguments << lit("--option") << lit("allow_unsandboxed_agent=true");
              if(customACP)
              {
                QJsonArray command;
                for(const QString &line : agentCommand->toPlainText().split(QLatin1Char('\n')))
                  if(!line.trimmed().isEmpty())
                    command.append(line.trimmed());
                if(command.isEmpty())
                {
                  QMessageBox::warning(&dialog, tr("Custom ACP"),
                                       tr("Enter the Agent executable and arguments."));
                  return;
                }
                arguments << lit("--command-json")
                          << QString::fromUtf8(
                                 QJsonDocument(command).toJson(QJsonDocument::Compact));
              }
            }
          }
          for(const QString &model : enabledModels)
            arguments << lit("--allowed-model") << model;
          if(!chosenDefault.isEmpty())
            arguments << lit("--default-model") << chosenDefault;
          if(defaultConnection->isChecked())
            arguments << lit("--select");
          arguments << lit("--json");

          const QString typedAPIKey = apiKey->text();
          const bool rememberCredential = usesAPIKey && rememberKey->isChecked();
          const bool sameProvider =
              !editing || existing.value(lit("preset")).toString() == presetId;
          // A session secret is scoped by stable profile id *and* provider. The dialog only
          // clears its visible field while editing; do not reuse the old provider's hidden
          // session secret when the same id is rebuilt for a different provider. Cancelling the
          // dialog leaves the old mapping untouched, as expected.
          const QString keyToStore =
              !typedAPIKey.isEmpty()
                  ? typedAPIKey
                  : sameProvider ? m_SessionSecrets.value(id) : QString();
          const bool makeActive = defaultConnection->isChecked();
          const DialogControlStates controls = beginDialogCommand();
          runCLIAsync(
              arguments, QByteArray(), 10000, &dialog,
              [&, controls, id, chosenDefault, usesAPIKey, typedAPIKey, rememberCredential, keyToStore,
               makeActive](int saveExit, const QByteArray &, const QString &saveError, bool) {
                if(saveExit != 0)
                {
                  finishDialogCommand(controls);
                  QMessageBox::critical(
                      &dialog, tr("Could not save AI connection"),
                      saveError.isEmpty() ? tr("The AI host rejected the settings.") : saveError);
                  return;
                }

                const auto completeProfileSave =
                    [&, controls, id, chosenDefault, usesAPIKey, typedAPIKey, rememberCredential,
                     keyToStore,
                     makeActive](bool credentialUpdated, const QString &credentialWarning) {
                      if(rememberCredential && !keyToStore.isEmpty())
                      {
                        if(credentialUpdated)
                          m_SessionSecrets.remove(id);
                        else
                          // Keep a typed replacement usable for this qrenderdoc session when the
                          // OS vault is unavailable or rejects the write. It still never enters
                          // argv or profile JSON.
                          m_SessionSecrets[id] = keyToStore;
                      }
                      else if(!rememberCredential)
                      {
                        if(usesAPIKey && !typedAPIKey.isEmpty())
                          m_SessionSecrets[id] = typedAPIKey;
                        else
                          m_SessionSecrets.remove(id);
                      }

                      finishDialogCommand(controls);
                      if(!credentialUpdated)
                      {
                        QMessageBox::warning(
                            &dialog, tr("Connection saved; credential not updated"),
                            credentialWarning.isEmpty()
                                ? tr("The connection was saved, but Windows Credential Manager "
                                     "could not be updated. Reopen this connection to retry or "
                                     "use a session-only key.")
                                : credentialWarning);
                      }
                      if(makeActive)
                      {
                        m_ActiveProfile = id;
                        m_ActiveModel = chosenDefault;
                      }
                      refreshPageProfiles(id);
                      refreshConnectionStatus();
                      dialog.accept();
                    };

                if(rememberCredential && !keyToStore.isEmpty())
                {
                  runCLIAsync(
                      // i18n-ignore: Fixed AgentHost CLI command and option identifiers; never shown to users.
                      {lit("profile"), lit("credential"), lit("store"), lit("--profile"), id,
                       lit("--api-key-stdin"), lit("--json")},
                      keyToStore.toUtf8(), 7000, &dialog,
                      [completeProfileSave](int storeExit, const QByteArray &,
                                            const QString &credentialWarning, bool) {
                        completeProfileSave(storeExit == 0, credentialWarning);
                      });
                }
                else if(!rememberCredential)
                {
                  deleteStoredCredential(
                      id, [completeProfileSave](bool deleted, const QString &deleteError) {
                        completeProfileSave(deleted, deleteError);
                      });
                }
                else
                {
                  // An empty field deliberately keeps an existing saved credential. For a new
                  // connection with no key, the profile is still saved but diagnostics will
                  // explain that authentication is missing.
                  completeProfileSave(true, QString());
                }
              });
        });

        dialog.exec();
        dialogClosing = true;
        const std::shared_ptr<SubscriptionProcessState> closingOperation = activeAuthOperation;
        if(closingOperation)
        {
          const QPointer<QProcess> process = closingOperation->process;
          if(process && process->state() != QProcess::NotRunning)
          {
            process->write("cancel\n");
            process->closeWriteChannel();
            closeKillOnCloseJob(closingOperation->job);
            process->kill();
          }
          if(!closingOperation->completionHandled)
          {
            closingOperation->completionHandled = true;
            closeKillOnCloseJob(closingOperation->job);
            if(activeAuthOperation == closingOperation)
              activeAuthOperation.reset();
            if(process)
            {
              disconnect(process, NULL, &dialog, NULL);
              closingOperation->process = NULL;
              if(process->state() == QProcess::NotRunning)
                process->deleteLater();
              else
              {
                process->setParent(QCoreApplication::instance());
                deleteProcessWhenFinished(process);
              }
            }
          }
          closeKillOnCloseJob(closingOperation->job);
        }
      };

  connect(add, &QPushButton::clicked, page, [=]() {
    if(*pageCommandRunning || !connectionMutationAllowed(page))
      return;
    editConnection(QJsonObject());
  });
  connect(edit, &QPushButton::clicked, page, [=]() {
    if(*pageCommandRunning || !connectionMutationAllowed(page))
      return;
    if(connections->currentItem())
      editConnection(profileById(connections->currentItem()->data(Qt::UserRole).toString()));
  });
  connect(connections, &QListWidget::itemDoubleClicked, page,
          [=](QListWidgetItem *) { edit->click(); });
  connect(remove, &QPushButton::clicked, page, [=]() {
    if(*pageCommandRunning || !connectionMutationAllowed(page))
      return;
    if(!connections->currentItem())
      return;
    const QString id = connections->currentItem()->data(Qt::UserRole).toString();
    const QString displayName = connectionDisplayName(profileById(id));
    if(QMessageBox::question(page, tr("Remove AI Connection"),
                             tr("Remove '%1'? Bread AI will forget its saved API credential, "
                                "session-only credential, and connection-scoped vendor state. A "
                                "vendor account sign-in stored by the provider or operating system "
                                "may remain.")
                                 .arg(displayName)) != QMessageBox::Yes)
      return;
    setPageCommandRunning(true);
    runCLIAsync(
        // i18n-ignore: Fixed AgentHost CLI command and option identifiers; never shown to users.
        {lit("profile"), lit("credential"), lit("delete"), lit("--profile"), id,
         lit("--json")}, QByteArray(),    // i18n-ignore: Fixed AgentHost CLI option identifier.
        5000, page,
        [=](int credentialExit, const QByteArray &, const QString &credentialError, bool) {
          if(credentialExit != 0)
          {
            setPageCommandRunning(false);
            QMessageBox::critical(
                page, tr("Remove failed"),
                credentialError.isEmpty()
                    ? tr("The saved credential could not be removed, so the connection was kept.")
                    : credentialError);
            return;
          }
          runCLIAsync(
              // i18n-ignore: Fixed AgentHost CLI command and option identifiers; never shown to users.
              {lit("profile"), lit("remove"), id, lit("--json")}, QByteArray(), 7000, page,
              [=](int exitCode, const QByteArray &, const QString &error, bool) {
                if(exitCode != 0)
                {
                  setPageCommandRunning(false);
                  QMessageBox::critical(page, tr("Remove failed"), error);
                  return;
                }
                m_SessionSecrets.remove(id);
                refreshPageProfiles(QString());
                refreshConnectionStatus();
              });
        });
  });
  connect(makeDefault, &QPushButton::clicked, page, [=]() {
    if(*pageCommandRunning || !connectionMutationAllowed(page))
      return;
    if(!connections->currentItem())
      return;
    const QString id = connections->currentItem()->data(Qt::UserRole).toString();
    setPageCommandRunning(true);
    runCLIAsync(
        // i18n-ignore: Fixed AgentHost CLI command and option identifiers; never shown to users.
        {lit("profile"), lit("select"), id, lit("--json")}, QByteArray(), 5000, page,
        [=](int exitCode, const QByteArray &, const QString &error, bool) {
          if(exitCode != 0)
          {
            setPageCommandRunning(false);
            QMessageBox::critical(page, tr("Default connection"), error);
            return;
          }
          m_ActiveProfile = id;
          m_ActiveModel = m_ProfileDefaultModels.value(id);
          refreshPageProfiles(id);
          refreshConnectionStatus();
        });
  });
  connect(check, &QPushButton::clicked, page, [=]() {
    if(*pageCommandRunning || !connections->currentItem())
      return;
    const QString id = connections->currentItem()->data(Qt::UserRole).toString();
    setPageCommandRunning(true);
    runConnectionCheck(page, id, true, [=]() { setPageCommandRunning(false); });
  });
  reloadList(m_ActiveProfile);
  // The Settings page can open before the assistant's initial asynchronous
  // profile refresh completes. Populate this page from the completed snapshot
  // rather than leaving an empty connection list until Settings is reopened.
  refreshProfiles(page, [this, reloadList]() { reloadList(m_ActiveProfile); });

  // Profile dialogs share one guarded asynchronous AgentHost command adapter. The Agent and tool
  // catalogs are not editable here: Skills come from the installed package and tools come from
  // the task-scoped first-party MCP server.
  const QPointer<AIAssistant> profileOwner(this);
  *profileCommandRunner = [profileOwner](const QStringList &arguments, const QByteArray &input,
                                         int timeoutMs, QObject *owner,
                                         const AIHostCommandCompletion &completion) {
    const QPointer<AIAssistant> assistant = profileOwner;
    if(!assistant || !owner)
    {
      if(!owner)
        return;
      AIHostCommandReply reply;
      reply.commandUnavailable = true;
      reply.diagnostic =
          QCoreApplication::translate("AIAssistant", "AI host was not found");
      QTimer::singleShot(0, owner, [completion, reply]() { completion(reply); });
      return;
    }
    assistant->runCLIAsync(
        arguments, input, timeoutMs, owner,
        [completion](int exitCode, const QByteArray &standardOutput,
                     const QString &diagnostic, bool unavailable) {
          AIHostCommandReply reply;
          reply.exitCode = exitCode;
          reply.standardOutput = standardOutput;
          reply.diagnostic = diagnostic;
          reply.commandUnavailable = unavailable;
          completion(reply);
        });
  };


  return page;
}


// ---------------------------------------------------------------- tool catalog
// The Agent decides which Skill a request needs; this section decides what is on the
// menu at all. Everything is stored by the AgentHost, so an API connection and an ACP
// subscription see exactly the same Skills and MCP servers.
QWidget *AIAssistant::createToolCatalogSection(QWidget *parent, QWidget *connectionsPage)
{
  QGroupBox *box = new QGroupBox(tr("Connections, Skills and tools"), parent);
  QVBoxLayout *layout = new QVBoxLayout(box);

  QLabel *status = new QLabel(tr("Loading Skills..."), box);
  status->setWordWrap(true);
  layout->addWidget(status);

  QTabWidget *tabs = new QTabWidget(box);
  tabs->setObjectName(lit("AIToolCatalogTabs"));    // i18n-ignore: automation id.
  if(connectionsPage != NULL)
  {
    connectionsPage->setParent(tabs);
    tabs->addTab(connectionsPage, tr("Connections"));
  }

  const auto makeTree = [](QWidget *page, const QString &automationId, const QStringList &headers) {
    QTreeWidget *tree = new QTreeWidget(page);
    tree->setObjectName(automationId);
    tree->setColumnCount(headers.size());
    tree->setHeaderLabels(headers);
    tree->setRootIsDecorated(false);
    tree->setUniformRowHeights(true);
    tree->setAllColumnsShowFocus(true);
    tree->setSelectionMode(QAbstractItemView::SingleSelection);
    tree->setMinimumHeight(200);
    tree->header()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
    tree->header()->setSectionResizeMode(headers.size() - 1, QHeaderView::Stretch);
    return tree;
  };

  // ------------------------------------------------------------------- Skills
  QWidget *skillPage = new QWidget(tabs);
  QVBoxLayout *skillLayout = new QVBoxLayout(skillPage);
  QTreeWidget *skills = makeTree(skillPage, lit("AISkillList"),    // i18n-ignore: automation id.
                                 {tr("Name"), tr("Source"), tr("What it is for")});
  skillLayout->addWidget(skills, 1);
  QHBoxLayout *skillButtons = new QHBoxLayout();
  QPushButton *openSkill = new QPushButton(tr("Edit..."), skillPage);
  QPushButton *newSkill = new QPushButton(tr("New..."), skillPage);
  QPushButton *deleteSkill = new QPushButton(tr("Delete"), skillPage);
  QPushButton *addFolder = new QPushButton(tr("Add Folder..."), skillPage);
  QPushButton *removeFolder = new QPushButton(tr("Remove Folder"), skillPage);
  skillButtons->addWidget(openSkill);
  skillButtons->addWidget(newSkill);
  skillButtons->addWidget(deleteSkill);
  skillButtons->addSpacing(12);
  skillButtons->addWidget(addFolder);
  skillButtons->addWidget(removeFolder);
  skillButtons->addStretch(1);
  skillLayout->addLayout(skillButtons);
  QLabel *skillHint = new QLabel(
      tr("A Skill is the procedure Bread AI follows for one kind of request. Editing one "
         "that came with RenderDoc saves your own copy, which is loaded from then on and "
         "survives updates. Type @ in the chat box to load one explicitly."),
      skillPage);
  skillHint->setWordWrap(true);
  skillLayout->addWidget(skillHint);
  tabs->addTab(skillPage, tr("Skills"));

  // ------------------------------------------------------------- MCP servers
  // An MCP server and an unpacker are configured differently and thought about
  // differently, so they are two tabs. They are still one list underneath: a server that
  // also happens to read assets keeps both roles rather than being registered twice.
  QWidget *mcpPage = new QWidget(tabs);
  QVBoxLayout *mcpLayout = new QVBoxLayout(mcpPage);
  auto builtinHint = new QLabel(tr("This list contains additional MCP servers. Bread provides its built-in Host, UEX asset reader and managed Unity connection automatically; a separate UEX MCP server is not required."), mcpPage);
  builtinHint->setWordWrap(true);
  mcpLayout->addWidget(builtinHint);
  QTreeWidget *mcpTools = makeTree(mcpPage, lit("AIMcpList"),    // i18n-ignore: automation id.
                                   {tr("Name"), tr("Connection"), tr("Used for"),
                                    tr("What it is for")});
  mcpLayout->addWidget(mcpTools, 1);
  // Each tab carries its own buttons: what applies to a server (reach it, test it) is
  // not what applies to a program (find it on disk, work out how to drive it).
  QHBoxLayout *mcpButtons = new QHBoxLayout();
  QPushButton *addServer = new QPushButton(tr("Add Server..."), mcpPage);
  QPushButton *editServer = new QPushButton(tr("Edit..."), mcpPage);
  QPushButton *removeServer = new QPushButton(tr("Remove"), mcpPage);
  QPushButton *testServer = new QPushButton(tr("Test Connection"), mcpPage);
  testServer->setToolTip(tr("Start or reach the selected server and list the tools it offers."));
  mcpButtons->addWidget(addServer);
  mcpButtons->addWidget(editServer);
  mcpButtons->addWidget(removeServer);
  mcpButtons->addWidget(testServer);
  mcpButtons->addStretch(1);
  mcpLayout->addLayout(mcpButtons);
  tabs->addTab(mcpPage, tr("MCP servers"));

  // --------------------------------------------------------------- Unpackers
  QWidget *toolPage = new QWidget(tabs);
  QVBoxLayout *toolLayout = new QVBoxLayout(toolPage);
  QTreeWidget *unpackers = makeTree(toolPage, lit("AIToolList"),    // i18n-ignore: automation id.
                                    {tr("Name"), tr("Program"), tr("Reads"),
                                     tr("What it is for")});
  toolLayout->addWidget(unpackers, 1);
  QLabel *toolHint = new QLabel(
      tr("uex, AssetStudioModCLI, UnityPy and umodel come with their command lines and asset "
         "formats already configured: open one and point it at the installed program. Any "
         "other program can be added as a custom unpacker."),
      toolPage);
  toolHint->setWordWrap(true);
  toolLayout->addWidget(toolHint);
  QHBoxLayout *toolButtons = new QHBoxLayout();
  QPushButton *addTool = new QPushButton(tr("Add Program..."), toolPage);
  QPushButton *detectTools = new QPushButton(tr("Find Installed..."), toolPage);
  detectTools->setToolTip(
      tr("Look on this computer (PATH, Program Files, common tool folders) for unpackers "
         "Bread AI already knows how to drive, and register the ones it finds."));
  QPushButton *editTool = new QPushButton(tr("Edit..."), toolPage);
  QPushButton *removeTool = new QPushButton(tr("Remove"), toolPage);
  QPushButton *testTool = new QPushButton(tr("Run Test"), toolPage);
  testTool->setToolTip(tr("Run the selected program once and check that it answers."));
  toolButtons->addWidget(addTool);
  toolButtons->addWidget(detectTools);
  toolButtons->addWidget(editTool);
  toolButtons->addWidget(removeTool);
  toolButtons->addWidget(testTool);
  toolButtons->addStretch(1);
  toolLayout->addLayout(toolButtons);
  tabs->addTab(toolPage, tr("Unpackers"));

  layout->addWidget(tabs, 1);

  const auto currentToolTree = [tabs, mcpPage, mcpTools, unpackers]() -> QTreeWidget * {
    return tabs->currentWidget() == mcpPage ? mcpTools : unpackers;
  };

  const std::shared_ptr<bool> updating = std::make_shared<bool>(false);
  const std::shared_ptr<QJsonArray> toolDefinitions = std::make_shared<QJsonArray>();
  const QPointer<QGroupBox> owner(box);

  const std::shared_ptr<QJsonArray> builtinUnpackers = std::make_shared<QJsonArray>();
  std::function<void()> reload = [this, owner, skills, mcpTools, unpackers, status, updating,
                                  toolDefinitions, builtinUnpackers]() {
    if(!owner)
      return;
    runCLIAsync(
        // i18n-ignore: Fixed AgentHost CLI command and option identifiers.
        {lit("tools"), lit("list"), lit("--json")}, QByteArray(), 15000, owner,
        [owner, skills, status, updating](int exitCode, const QByteArray &output,
                                          const QString &error, bool unavailable) {
          if(!owner)
            return;
          if(exitCode != 0 || unavailable)
          {
            setDiagnosticStatus(status, DiagnosticLevel::Error,
                                error.isEmpty()
                                    ? QCoreApplication::translate(
                                          "AIAssistant", "Could not read the Skill catalog.")
                                    : error);
            return;
          }
          const QJsonObject view = QJsonDocument::fromJson(output).object();
          *updating = true;
          skills->clear();
          for(const QJsonValue &value : view.value(lit("skills")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
          {
            const QJsonObject skill = value.toObject();
            const QString id = skill.value(lit("id")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            if(id.isEmpty())
              continue;
            // Label the shipped skill family as built-in even when the user
            // keeps an edited override. Actual path/removability below retain
            // the override's ownership; no user edits are replaced or deleted.
            const QDir application(QCoreApplication::applicationDirPath());
            const bool shipped = id.indexOf(QLatin1Char('/')) < 0 &&
                                 id.indexOf(QLatin1Char('\\')) < 0 &&
                (QFileInfo(application.filePath(lit("replaylab/skills/%1/SKILL.md").arg(id))).isFile() ||    // i18n-ignore: fixed application-owned skill path.
                 QFileInfo(application.filePath(lit("replaylab/portable-skills/%1/SKILL.md").arg(id))).isFile());    // i18n-ignore: fixed application-owned adapter path.
            const bool bundled = shipped || skill.value(lit("source")).toString() != lit("user");    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            QTreeWidgetItem *item = new QTreeWidgetItem(skills);
            const QString title = skill.value(lit("title")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setText(0, title.isEmpty() ? id : title);
            item->setText(1, bundled ? QCoreApplication::translate("AIAssistant", "Built in")
                                     : QCoreApplication::translate("AIAssistant", "Yours"));
            item->setToolTip(1, skill.value(lit("path")).toString());    // i18n-ignore: source path diagnostic, not UI prose.
            item->setText(2, skill.value(lit("description")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setToolTip(0, id);
            item->setToolTip(2, skill.value(lit("description")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setData(0, Qt::UserRole, id);
            item->setData(0, Qt::UserRole + 1, skill.value(lit("path")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setData(0, Qt::UserRole + 2, skill.value(lit("root")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setData(0, Qt::UserRole + 3, skill.value(lit("removable")).toBool());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
            item->setCheckState(0, skill.value(lit("enabled")).toBool() ? Qt::Checked    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                                                                       : Qt::Unchecked);
          }
          *updating = false;
          const QStringList roots = [&view]() {
            QStringList result;
            for(const QJsonValue &value : view.value(lit("skillRoots")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
              result << value.toString();
            return result;
          }();
          setDiagnosticStatus(
              status, DiagnosticLevel::Success,
              roots.isEmpty()
                  ? QCoreApplication::translate("AIAssistant",
                                                "%1 Skills from the installed package.")
                        .arg(skills->topLevelItemCount())
                  : QCoreApplication::translate("AIAssistant", "%1 Skills; extra folders: %2")
                        .arg(skills->topLevelItemCount())
                        .arg(roots.join(lit(", "))));
          status->setToolTip(QCoreApplication::translate(
              "AIAssistant", "Bundled skills and plugin dependencies load from the application directory. Extra folders contain optional user overrides; other computers do not require those folders."));
        });
    runCLIAsync(
        // i18n-ignore: Fixed AgentHost CLI command and option identifiers.
        {lit("tools"), lit("tool-list"), lit("--json")}, QByteArray(), 15000, owner,
        [this, owner, mcpTools, unpackers, updating, toolDefinitions, builtinUnpackers](
            int exitCode, const QByteArray &output, const QString &, bool unavailable) {
          if(!owner || exitCode != 0 || unavailable)
            return;
          const QJsonObject view = QJsonDocument::fromJson(output).object();
          *updating = true;
          mcpTools->clear();
          unpackers->clear();
          // Built-in coverage first, so "do I even need a tool for this?" has an answer
          // on screen instead of being discovered when a task fails.
          for(const QJsonValue &value : view.value(lit("builtinReaders")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
          {
            const QJsonObject builtin = value.toObject();
            QTreeWidgetItem *item = new QTreeWidgetItem(unpackers);
            item->setText(0, builtin.value(lit("title")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setText(1, QCoreApplication::translate("AIAssistant", "Built in"));
            item->setText(2, QCoreApplication::translate("AIAssistant", "Reading assets"));
            item->setText(3, builtin.value(lit("description")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            const QString limits = builtin.value(lit("limits")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            if(!limits.isEmpty())
              item->setToolTip(3, QCoreApplication::translate("AIAssistant", "Cannot read: %1")
                                      .arg(limits));
            item->setFlags(item->flags() & ~Qt::ItemIsUserCheckable);
          }
          // The unpackers that ship configured. Each is one row whether or not a
          // program has been chosen yet, so the list answers "which tools exist and
          // what do they read" before anything is set up.
          *builtinUnpackers = view.value(lit("builtinUnpackers")).toArray();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
          m_BuiltinUnpackerCatalog = *builtinUnpackers;
          for(const QJsonValue &value : *builtinUnpackers)
          {
            const QJsonObject unpacker = value.toObject();
            const bool configured = unpacker.value(lit("configured")).toBool();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            QTreeWidgetItem *item = new QTreeWidgetItem(unpackers);
            item->setText(0, unpacker.value(lit("title")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setText(1, configured ? unpacker.value(lit("program")).toString()    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                                        : QCoreApplication::translate(
                                              "AIAssistant", "Program not set - double-click"));
            QStringList formats;
            for(const QJsonValue &pattern : unpacker.value(lit("patterns")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
              formats << pattern.toString();
            item->setText(2, formats.join(QLatin1Char(' ')));
            item->setText(3, unpacker.value(lit("reads")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setToolTip(0, unpacker.value(lit("homepage")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setToolTip(2, formats.join(QLatin1Char('\n')));
            item->setToolTip(3, unpacker.value(lit("reads")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setData(0, Qt::UserRole, configured ? unpacker.value(lit("id")).toString()    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                                                     : QString());
            item->setData(0, Qt::UserRole + 1, lit("builtin-unpacker"));    // i18n-ignore: row kind marker.
            item->setData(0, Qt::UserRole + 2, unpacker.value(lit("kind")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            if(configured)
            {
              item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
              item->setCheckState(0, unpacker.value(lit("enabled")).toBool(true) ? Qt::Checked    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                                                                                : Qt::Unchecked);
            }
            else
            {
              item->setFlags(item->flags() & ~Qt::ItemIsUserCheckable);
              item->setForeground(1, QBrush(QColor(0x9a, 0x9a, 0x9a)));
            }
          }
          // Servers the AI registered through update_bread_configuration (and older
          // hand-written entries) live in agent-tools.json, not in the tool list. They
          // belong in this tab with the same switch, or the user cannot see or stop
          // what the AI attached.
          for(const QJsonValue &value : view.value(lit("mcpServers")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
          {
            const QJsonObject server = value.toObject();
            const QString id = server.value(lit("id")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            if(id.isEmpty())
              continue;
            QTreeWidgetItem *item = new QTreeWidgetItem(mcpTools);
            const QString title = server.value(lit("title")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setText(0, title.isEmpty() ? id : title);
            const bool http = server.value(lit("transport")).toString() == lit("http");    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setText(1, http ? QCoreApplication::translate("AIAssistant", "MCP (HTTP)")
                                  : QCoreApplication::translate("AIAssistant", "MCP (program)"));
            item->setText(2, QCoreApplication::translate("AIAssistant", "Tools it describes itself"));
            item->setText(3, server.value(lit("note")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            QStringList detail;
            detail << (http ? server.value(lit("url")).toString()    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                            : server.value(lit("command")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            for(const QJsonValue &argument : server.value(lit("args")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
              detail << argument.toString();
            item->setToolTip(0, id);
            item->setToolTip(1, detail.join(QLatin1Char(' ')));
            item->setData(0, Qt::UserRole, id);
            item->setData(0, Qt::UserRole + 1, lit("mcp-server"));    // i18n-ignore: row kind marker.
            item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
            item->setCheckState(0, server.value(lit("enabled")).toBool(true) ? Qt::Checked    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                                                                             : Qt::Unchecked);
          }
          *toolDefinitions = view.value(lit("tools")).toArray();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
          for(const QJsonValue &value : *toolDefinitions)
          {
            const QJsonObject tool = value.toObject();
            const QString id = tool.value(lit("id")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            if(id.isEmpty())
              continue;
            // A configured built-in already has its row above.
            if(!tool.value(lit("builtinKind")).toString().isEmpty())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
              continue;
            // Where a tool is listed follows how it is reached, which is also what
            // deciding to configure it feels like: a URL or a command that speaks MCP,
            // or a program somebody has to be taught to drive.
            const bool speaksMcp =
                tool.value(lit("connection")).toObject()    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                    .value(lit("kind")).toString().startsWith(lit("mcp"));    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            QTreeWidgetItem *item = new QTreeWidgetItem(speaksMcp ? mcpTools : unpackers);
            const QString title = tool.value(lit("title")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setText(0, title.isEmpty() ? id : title);
            item->setText(1, connectionSummary(tool.value(lit("connection")).toObject()));    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setText(2, toolRoleSummary(tool));
            item->setText(3, tool.value(lit("description")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setToolTip(0, id);
            item->setToolTip(1, connectionDetail(tool.value(lit("connection")).toObject()));    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setToolTip(3, tool.value(lit("interface")).toObject()    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                                    .value(lit("note")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            item->setData(0, Qt::UserRole, id);
            item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
            item->setCheckState(0, tool.value(lit("enabled")).toBool() ? Qt::Checked    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                                                                      : Qt::Unchecked);
          }
          *updating = false;

        });
  };

  const std::function<void(const QStringList &)> runToolCommand =
      [this, owner, reload](const QStringList &arguments) {
        if(!owner)
          return;
        runCLIAsync(arguments, QByteArray(), 30000, owner,
                    [owner, reload](int, const QByteArray &, const QString &, bool) {
                      if(owner)
                        reload();
                    });
      };

  QObject::connect(skills, &QTreeWidget::itemChanged, box,
                   [updating, runToolCommand](QTreeWidgetItem *item, int column) {
                     if(*updating || !item || column != 0)
                       return;
                     const QString id = item->data(0, Qt::UserRole).toString();
                     if(id.isEmpty())
                       return;
                     runToolCommand({lit("tools"),    // i18n-ignore: AgentHost CLI command
                                     item->checkState(0) == Qt::Checked ? lit("skill-enable")
                                                                        : lit("skill-disable"),    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                                     id});
                   });
  // Both lists switch tools on and off the same way, so the same handler serves both;
  // a server row from agent-tools.json takes the server commands.
  const auto isServerRow = [](QTreeWidgetItem *item) {
    return item && item->data(0, Qt::UserRole + 1).toString() == lit("mcp-server");    // i18n-ignore: row kind marker.
  };
  for(QTreeWidget *tree : {mcpTools, unpackers})
    QObject::connect(tree, &QTreeWidget::itemChanged, box,
                     [updating, runToolCommand, isServerRow](QTreeWidgetItem *item, int column) {
                       if(*updating || !item || column != 0)
                         return;
                       const QString id = item->data(0, Qt::UserRole).toString();
                       if(id.isEmpty())
                         return;
                       const bool on = item->checkState(0) == Qt::Checked;
                       // i18n-ignore: AgentHost CLI command identifiers.
                       const QString verb = isServerRow(item)
                                                ? (on ? lit("mcp-enable") : lit("mcp-disable")) // i18n-ignore: AgentHost CLI commands.
                                                : (on ? lit("tool-enable") : lit("tool-disable")); // i18n-ignore: AgentHost CLI commands.
                       runToolCommand({lit("tools"), verb, id});    // i18n-ignore: AgentHost CLI command
                     });

  const std::function<void()> openSelectedSkill = [this, box, skills, reload]() {
    QTreeWidgetItem *item = skills->currentItem();
    if(!item)
      return;
    const QString id = item->data(0, Qt::UserRole).toString();
    if(id.isEmpty())
      return;
    showSkillDocument(box, id, reload);
  };
  QObject::connect(openSkill, &QPushButton::clicked, box, openSelectedSkill);
  QObject::connect(skills, &QTreeWidget::itemDoubleClicked, box,
                   [openSelectedSkill](QTreeWidgetItem *, int) { openSelectedSkill(); });
  QObject::connect(newSkill, &QPushButton::clicked, box, [this, box, runToolCommand]() {
    QDialog dialog(box);
    dialog.setWindowTitle(tr("New Skill"));
    QFormLayout *form = new QFormLayout(&dialog);
    QLineEdit *identifier = new QLineEdit(&dialog);
    form->addRow(tr("Skill id"), identifier);
    QLabel *hint = new QLabel(
        tr("Lower-case letters, digits, '.', '-' or '_'. The Skill is created from a template in "
           "your own Skill folder, ready to edit here."),
        &dialog);
    hint->setWordWrap(true);
    form->addRow(hint);
    QDialogButtonBox *buttons =
        new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
    form->addRow(buttons);
    QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    if(RDDialog::show(&dialog) != QDialog::Accepted || identifier->text().trimmed().isEmpty())
      return;
    runToolCommand({lit("tools"), lit("skill-new"),    // i18n-ignore: CLI command
                    identifier->text().trimmed().toLower()});
  });
  // Deleting one Skill, not the folder it lives in: removing the folder took every
  // Skill in it, which is why a test Skill could not be got rid of.
  QObject::connect(deleteSkill, &QPushButton::clicked, box, [box, skills, runToolCommand]() {
    QTreeWidgetItem *item = skills->currentItem();
    if(!item)
      return;
    const QString id = item->data(0, Qt::UserRole).toString();
    if(id.isEmpty())
      return;
    if(!item->data(0, Qt::UserRole + 3).toBool())
    {
      RDDialog::information(
          box, QCoreApplication::translate("AIAssistant", "Delete Skill"),
          QCoreApplication::translate(
              "AIAssistant",
              "%1 came with RenderDoc. Switch it off instead; the installer owns the file.")
              .arg(id));
      return;
    }
    if(RDDialog::question(
           box, QCoreApplication::translate("AIAssistant", "Delete Skill"),
           QCoreApplication::translate("AIAssistant", "Delete the Skill %1 from disk?").arg(id)) !=
       QMessageBox::Yes)
      return;
    runToolCommand({lit("tools"), lit("skill-remove"), id});    // i18n-ignore: CLI command
  });
  QObject::connect(addFolder, &QPushButton::clicked, box, [this, box, runToolCommand]() {
    const QString folder = RDDialog::getExistingDirectory(box, tr("Choose a folder of Skills"));
    if(folder.isEmpty())
      return;
    runToolCommand({lit("tools"), lit("add-skill-root"), folder});    // i18n-ignore: CLI command
  });
  QObject::connect(removeFolder, &QPushButton::clicked, box, [box, skills, runToolCommand]() {
    QTreeWidgetItem *item = skills->currentItem();
    if(!item)
      return;
    // The registered root comes from the configuration, never from the document path.
    const QString root = item->data(0, Qt::UserRole + 2).toString();
    if(root.isEmpty())
      return;
    if(RDDialog::question(
           box, QCoreApplication::translate("AIAssistant", "Remove Skill folder"),
           QCoreApplication::translate("AIAssistant", "Stop offering Skills from %1?").arg(root)) !=
       QMessageBox::Yes)
      return;
    runToolCommand({lit("tools"), lit("remove-skill-root"), root});    // i18n-ignore: CLI command
  });

  const auto isBuiltinUnpackerRow = [](QTreeWidgetItem *item) {
    return item && item->data(0, Qt::UserRole + 1).toString() == lit("builtin-unpacker");    // i18n-ignore: row kind marker.
  };
  const std::function<void()> openSelectedTool = [this, box, toolPage, currentToolTree,
                                                 toolDefinitions, isServerRow,
                                                 isBuiltinUnpackerRow, builtinUnpackers]() {
    QTreeWidgetItem *item = currentToolTree()->currentItem();
    if(!item)
      return;
    if(isBuiltinUnpackerRow(item))
    {
      const QString kind = item->data(0, Qt::UserRole + 2).toString();
      for(const QJsonValue &value : *builtinUnpackers)
        if(value.toObject().value(lit("kind")).toString() == kind)    // i18n-ignore: AgentHost protocol identifier, not UI prose.
        {
          editBuiltinUnpacker(toolPage, value.toObject());
          return;
        }
      return;
    }
    const QString id = item->data(0, Qt::UserRole).toString();
    if(id.isEmpty())
      return;    // a built-in reader row: nothing to edit.
    if(isServerRow(item))
    {
      RDDialog::information(
          box, QCoreApplication::translate("AIAssistant", "MCP server"),
          QCoreApplication::translate(
              "AIAssistant",
              "%1 was registered as a server entry (by Bread AI or by hand). Switch it "
              "off or remove it here; to change how it is reached, remove it and add it "
              "again with Add Server, or ask Bread AI to update it.")
              .arg(id));
      return;
    }
    for(const QJsonValue &value : *toolDefinitions)
      if(value.toObject().value(lit("id")).toString() == id)    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      {
        editToolProvider(toolPage, value.toObject());
        return;
      }
  };
  // Each tab has its own dialog. Add Program offers the shipped unpackers first
  // (one path each) and a custom program last; Add Server asks how to reach a server.
  QObject::connect(addTool, &QPushButton::clicked, box, [this, toolPage, builtinUnpackers]() {
    QDialog chooser(toolPage);
    chooser.setWindowTitle(tr("Add Program"));
    QVBoxLayout *chooserLayout = new QVBoxLayout(&chooser);
    chooserLayout->addWidget(new QLabel(tr("Which unpacker?"), &chooser));
    QListWidget *choices = new QListWidget(&chooser);
    for(const QJsonValue &value : *builtinUnpackers)
    {
      const QJsonObject unpacker = value.toObject();
      QListWidgetItem *entry = new QListWidgetItem(unpacker.value(lit("title")).toString(), choices);    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      entry->setToolTip(unpacker.value(lit("reads")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      entry->setData(Qt::UserRole, unpacker.value(lit("kind")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    }
    QListWidgetItem *custom = new QListWidgetItem(tr("Custom program..."), choices);
    custom->setData(Qt::UserRole, QString());
    choices->setCurrentRow(0);
    chooserLayout->addWidget(choices);
    QDialogButtonBox *chooserButtons =
        new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &chooser);
    chooserLayout->addWidget(chooserButtons);
    QObject::connect(chooserButtons, &QDialogButtonBox::accepted, &chooser, &QDialog::accept);
    QObject::connect(chooserButtons, &QDialogButtonBox::rejected, &chooser, &QDialog::reject);
    QObject::connect(choices, &QListWidget::itemDoubleClicked, &chooser,
                     [&chooser](QListWidgetItem *) { chooser.accept(); });
    if(RDDialog::show(&chooser) != QDialog::Accepted || !choices->currentItem())
      return;
    const QString kind = choices->currentItem()->data(Qt::UserRole).toString();
    if(kind.isEmpty())
    {
      QJsonObject seed;
      QJsonObject connection;
      connection[lit("kind")] = lit("cli");    // i18n-ignore: protocol value.
      seed[lit("connection")] = connection;    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      editToolProvider(toolPage, seed, ToolDialogMode::Program);
      return;
    }
    for(const QJsonValue &value : *builtinUnpackers)
      if(value.toObject().value(lit("kind")).toString() == kind)    // i18n-ignore: AgentHost protocol identifier, not UI prose.
        editBuiltinUnpacker(toolPage, value.toObject());
  });
  QObject::connect(addServer, &QPushButton::clicked, box, [this, mcpPage]() {
    QJsonObject seed;
    QJsonObject connection;
    connection[lit("kind")] = lit("mcp-stdio");    // i18n-ignore: protocol value.
    seed[lit("connection")] = connection;    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    editToolProvider(mcpPage, seed, ToolDialogMode::Server);
  });
  QObject::connect(editTool, &QPushButton::clicked, box, openSelectedTool);
  QObject::connect(editServer, &QPushButton::clicked, box, openSelectedTool);
  for(QTreeWidget *tree : {mcpTools, unpackers})
    QObject::connect(tree, &QTreeWidget::itemDoubleClicked, box,
                     [openSelectedTool](QTreeWidgetItem *, int) { openSelectedTool(); });
  QObject::connect(detectTools, &QPushButton::clicked, box,
                   [this, toolPage]() { detectAssetReaders(toolPage); });
  const std::function<void()> removeSelectedTool = [box, currentToolTree, runToolCommand,
                                                    isServerRow, isBuiltinUnpackerRow]() {
    QTreeWidgetItem *item = currentToolTree()->currentItem();
    if(!item)
      return;
    const QString id = item->data(0, Qt::UserRole).toString();
    const bool server = isServerRow(item);
    if(id.isEmpty())
    {
      RDDialog::information(
          box, QCoreApplication::translate("AIAssistant", "Remove tool"),
          isBuiltinUnpackerRow(item)
              ? QCoreApplication::translate(
                    "AIAssistant", "No program is set for it yet, so there is nothing to remove.")
              : QCoreApplication::translate(
                    "AIAssistant", "That reader is built into RenderDoc and is always on."));
      return;
    }
    if(RDDialog::question(
           box, QCoreApplication::translate("AIAssistant", "Remove tool"),
           QCoreApplication::translate("AIAssistant", "Remove the tool %1?").arg(id)) !=
       QMessageBox::Yes)
      return;
    runToolCommand({lit("tools"), server ? lit("mcp-remove") : lit("tool-remove"), id});    // i18n-ignore: CLI command
  };
  QObject::connect(removeTool, &QPushButton::clicked, box, removeSelectedTool);
  QObject::connect(removeServer, &QPushButton::clicked, box, removeSelectedTool);
  // A configured tool is not a working tool. This is the button that says which.
  // The status line under the page is easy to miss, so the outcome is also shown in a
  // dialog, and the button says what it is doing while it waits.
  const auto testSelectedTool = [this, owner, box, currentToolTree, status, isServerRow,
                                 isBuiltinUnpackerRow](QPushButton *testTool) {
    QTreeWidgetItem *item = currentToolTree()->currentItem();
    if(!item)
    {
      RDDialog::information(
          box, QCoreApplication::translate("AIAssistant", "Test"),
          QCoreApplication::translate("AIAssistant", "Select a server or a program first."));
      return;
    }
    const QString id = item->data(0, Qt::UserRole).toString();
    if(id.isEmpty())
    {
      RDDialog::information(
          box, QCoreApplication::translate("AIAssistant", "Test"),
          isBuiltinUnpackerRow(item)
              ? QCoreApplication::translate("AIAssistant",
                                            "Set the program for it first (double-click the row).")
              : QCoreApplication::translate(
                    "AIAssistant", "That reader is built into RenderDoc; it needs no test."));
      return;
    }
    const bool server = isServerRow(item);
    const QString idle = testTool->text();
    testTool->setEnabled(false);
    testTool->setText(QCoreApplication::translate("AIAssistant", "Testing..."));
    setDiagnosticStatus(status, DiagnosticLevel::Info,
                        QCoreApplication::translate("AIAssistant", "Starting %1...").arg(id));
    const QPointer<QWidget> dialogParent(box);
    runCLIAsync(
        // i18n-ignore: Fixed AgentHost CLI command and option identifiers.
        {lit("tools"), server ? lit("mcp-test") : lit("tool-test"), id, lit("--json")},
        QByteArray(), 90000, owner,
        [owner, status, testTool, id, idle, server, dialogParent](
            int exitCode, const QByteArray &output, const QString &error, bool unavailable) {
          if(!owner)
            return;
          testTool->setEnabled(true);
          testTool->setText(idle);
          QString message;
          bool success = false;
          if(exitCode != 0 || unavailable)
          {
            message = error.isEmpty()
                          ? QCoreApplication::translate("AIAssistant", "%1 could not be tested.")
                                .arg(id)
                          : error;
          }
          else
          {
            const QJsonObject report = QJsonDocument::fromJson(output).object();
            success = report.value(lit("ok")).toBool();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            if(success && server)
              message = QCoreApplication::translate("AIAssistant",
                                                    "%1 answered and offers %2 tool(s) (%3 s).")
                            .arg(id)
                            .arg(report.value(lit("tools")).toInt())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                            .arg(report.value(lit("seconds")).toDouble());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            else if(success)
              message = QCoreApplication::translate("AIAssistant",
                                                    "%1 answered with %2 operations in %3s.")
                            .arg(id)
                            .arg(report.value(lit("operations")).toInt())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                            .arg(report.value(lit("seconds")).toDouble());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            else
              message = QCoreApplication::translate("AIAssistant", "%1 did not start: %2")
                            .arg(id, report.value(lit("error")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
          }
          setDiagnosticStatus(status, success ? DiagnosticLevel::Success : DiagnosticLevel::Error,
                              message);
          if(!dialogParent)
            return;
          // Never open a modal window inside the command runner's own callback.
          QTimer::singleShot(0, dialogParent, [dialogParent, message, success]() {
            if(!dialogParent)
              return;
            if(success)
              RDDialog::information(
                  dialogParent, QCoreApplication::translate("AIAssistant", "Test passed"), message);
            else
              RDDialog::critical(dialogParent,
                                 QCoreApplication::translate("AIAssistant", "Test failed"), message);
          });
        });
  };
  QObject::connect(testTool, &QPushButton::clicked, box,
                   [testSelectedTool, testTool]() { testSelectedTool(testTool); });
  QObject::connect(testServer, &QPushButton::clicked, box,
                   [testSelectedTool, testServer]() { testSelectedTool(testServer); });
  m_ToolCatalogReload = reload;
  QObject::connect(box, &QObject::destroyed, this, [this]() {
    m_ToolCatalogReload = std::function<void()>();
    refreshToolMentionCatalog();
  });
  reload();
  return box;
}

QString AIAssistant::connectionSummary(const QJsonObject &connection)
{
  // i18n-ignore: AgentHost connection kind identifiers.
  const QString kind = connection.value(lit("kind")).toString();
  if(kind == lit("mcp-stdio"))    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    return QCoreApplication::translate("AIAssistant", "MCP (program)");
  if(kind == lit("mcp-http"))    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    return QCoreApplication::translate("AIAssistant", "MCP (HTTP)");
  return QCoreApplication::translate("AIAssistant", "Command line");
}

QString AIAssistant::connectionDetail(const QJsonObject &connection)
{
  // i18n-ignore: AgentHost connection field names.
  if(connection.value(lit("kind")).toString() == lit("mcp-http"))
    return connection.value(lit("url")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  QStringList parts;
  parts << connection.value(lit("command")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  for(const QJsonValue &value : connection.value(lit("args")).toArray())
    parts << value.toString();
  return parts.join(QLatin1Char(' '));
}

QString AIAssistant::toolRoleSummary(const QJsonObject &tool)
{
  // A tool that has not been asked what it can do yet is worth saying so about: it is
  // the difference between "configured" and "usable".
  QStringList parts;
  // i18n-ignore: AgentHost role identifiers.
  const QJsonArray roles = tool.value(lit("roles")).toArray();
  bool reader = false;
  for(const QJsonValue &value : roles)
  {
    if(value.toString() == lit("asset-reader"))    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    {
      reader = true;
      parts << QCoreApplication::translate("AIAssistant", "Reading assets");
    }
    else if(value.toString() == lit("general"))    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      parts << QCoreApplication::translate("AIAssistant", "AI tasks");
  }
  if(reader)
  {
    bool proved = false;
    for(const QJsonValue &value :
        tool.value(lit("interface")).toObject().value(lit("operations")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      if(value.toObject().value(lit("role")).toString() == lit("asset-reader") &&
         !value.toObject().value(lit("proved")).isNull())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
        proved = true;
    parts << (proved ? QCoreApplication::translate("AIAssistant", "verified")
                     : QCoreApplication::translate("AIAssistant", "not verified yet"));
  }
  // i18n-ignore: Separator glyph is presentation, not prose.
  return parts.join(lit(", "));
}

void AIAssistant::askAIToConfigure(QWidget *parent)
{
  QDialog dialog(parent);
  dialog.setWindowTitle(tr("Let Bread AI set this up"));
  dialog.setMinimumWidth(620);
  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QLabel *intro = new QLabel(
      tr("Describe what you want available and where it is. Bread AI reads the current "
         "configuration, registers what is needed, works out how to call it and tells you what "
         "it changed. Everything it does here is reversible in this panel."),
      &dialog);
  intro->setWordWrap(true);
  layout->addWidget(intro);
  QPlainTextEdit *request = new QPlainTextEdit(&dialog);
  request->setObjectName(lit("AIConfigureRequest"));    // i18n-ignore: automation id.
  request->setPlaceholderText(
      tr("For example: my unpacked assets are in E:\\Game_Unpacked and my extractor is "
         "D:\\tools\\uex\\uex.exe - set it up so asset matching works."));
  request->setMinimumHeight(110);
  layout->addWidget(request, 1);
  QDialogButtonBox *buttons =
      new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
  buttons->button(QDialogButtonBox::Ok)->setText(tr("Ask"));
  layout->addWidget(buttons);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  if(RDDialog::show(&dialog) != QDialog::Accepted)
    return;
  const QString text = request->toPlainText().trimmed();
  if(text.isEmpty() || m_Prompt == NULL)
    return;
  // The request goes into the composer rather than being sent: the person sees exactly
  // what is being asked, and configuration is theirs to authorise.
  const QString existing = m_Prompt->toPlainText();
  m_Prompt->setPlainText(existing.isEmpty() ? text : existing + lit("\n\n") + text);
  m_Prompt->setFocus();
  m_Prompt->moveCursor(QTextCursor::End);
}

void AIAssistant::askAIToCompleteUnpacker(QWidget *parent, const QJsonObject &tool)
{
  // Configuring an unpacker by hand means knowing its arguments. Nobody knows the
  // arguments of a tool they just installed, so this hands the job to Bread AI with
  // the one thing it cannot obtain by itself: real files to try the program against.
  const QString id = tool.value(lit("id")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  const QString title = tool.value(lit("title")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  const QString program =
      tool.value(lit("connection")).toObject().value(lit("command")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.

  QDialog dialog(parent);
  dialog.setWindowTitle(tr("Let Bread AI work out %1").arg(title.isEmpty() ? id : title));
  dialog.setMinimumWidth(640);
  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QLabel *intro = new QLabel(
      tr("Bread AI will try this program against files you point it at: its help output, "
         "then real invocations, until something works. What worked is written into the tool's "
         "configuration as a named operation with typed parameters, so from then on it is "
         "called directly instead of being worked out again. What did not work is written down "
         "too, so the same dead end is not walked into on every asset."),
      &dialog);
  intro->setWordWrap(true);
  layout->addWidget(intro);

  QHBoxLayout *sampleRow = new QHBoxLayout();
  QLineEdit *samples = new QLineEdit(&dialog);
  samples->setObjectName(lit("AIUnpackerSamples"));    // i18n-ignore: automation id.
  samples->setPlaceholderText(tr("A folder or a file this unpacker should be able to read"));
  QPushButton *browse = new QPushButton(tr("Browse..."), &dialog);
  sampleRow->addWidget(samples, 1);
  sampleRow->addWidget(browse);
  layout->addLayout(sampleRow);
  QObject::connect(browse, &QPushButton::clicked, &dialog, [&dialog, samples]() {
    const QString chosen = RDDialog::getExistingDirectory(
        &dialog, QCoreApplication::translate("AIAssistant", "Assets to try it on"));
    if(!chosen.isEmpty())
      samples->setText(chosen);
  });

  QLabel *purposeLabel = new QLabel(tr("What is this tool for? (optional)"), &dialog);
  layout->addWidget(purposeLabel);
  QPlainTextEdit *purpose = new QPlainTextEdit(&dialog);
  purpose->setObjectName(lit("AIUnpackerPurpose"));    // i18n-ignore: automation id.
  purpose->setPlaceholderText(
      tr("For example: handles Unreal 4.27 .uasset - Texture2D and StaticMesh work, "
         "SkeletalMesh crashes this build."));
  // The tool's own description, the field the Add/Edit dialog writes. "note" is the
  // interface block's field and is never present on a registered tool.
  purpose->setPlainText(tool.value(lit("description")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  purpose->setMinimumHeight(80);
  layout->addWidget(purpose);

  QDialogButtonBox *buttons =
      new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
  buttons->button(QDialogButtonBox::Ok)->setText(tr("Start"));
  layout->addWidget(buttons);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  if(RDDialog::show(&dialog) != QDialog::Accepted)
    return;

  const QString sampleText = samples->text().trimmed();
  const QString purposeText = purpose->toPlainText().trimmed();
  if(m_Prompt == NULL || m_Send == NULL)
    return;

  QStringList request;
  request << tr("Work out how to drive the registered unpacker \"%1\" and write the result "
                "into its configuration.").arg(id);
  if(!program.isEmpty())
    request << tr("Program: %1").arg(program);
  if(!sampleText.isEmpty())
    request << tr("Try it on real files here: %1").arg(sampleText);
  if(!purposeText.isEmpty())
    request << tr("What the user says it is for: %1").arg(purposeText);
  request << tr("Use discover_tool_interface to probe it, and run_asset_tool_operation to try "
                "invocations. Help output is a hint, not proof: an operation counts only once "
                "it has produced the right result from one of those real files. Record each one "
                "with record_asset_tool_operation, giving it a name, a one-sentence description "
                "and a typed inputSchema, and record what fails as knownFailures. Tell me what "
                "it can and cannot do when you are done.");

  // Started in its own conversation so the run is visible and reviewable on its own,
  // and deferred by one turn so the settings dialog this was launched from has finished
  // closing before a new conversation replaces what is on screen.
  const QString prompt = request.join(lit("\n"));
  QPointer<AIAssistant> self(this);
  QTimer::singleShot(0, this, [self, prompt]() {
    if(!self || self->m_Prompt == NULL || self->m_Send == NULL)
      return;
    self->createConversation();
    self->m_Prompt->setPlainText(prompt);
    self->m_Prompt->setFocus();
    self->m_Send->click();
  });
}

void AIAssistant::showSkillDocument(QWidget *parent, const QString &skillId,
                                    const std::function<void()> &reload)
{
  // A Skill is the instruction the AI actually follows, so Settings shows the document
  // itself rather than a summary of it. A bundled Skill is replaced by the next install:
  // saving one copies it into the user folder and that copy wins from then on.
  const QPointer<QWidget> owner(parent);
  runCLIAsync(
      // i18n-ignore: Fixed AgentHost CLI command and option identifiers.
      {lit("tools"), lit("skill-show"), skillId, lit("--json")}, QByteArray(), 15000, parent,
      [this, owner, skillId, reload](int exitCode, const QByteArray &output, const QString &error,
                                     bool unavailable) {
        if(!owner)
          return;
        // The command runner is still unwinding this call: a modal window here would run a
        // nested event loop inside it. Everything that can block waits for the next turn.
        const QString diagnostic =
            (exitCode != 0 || unavailable)
                ? (error.isEmpty() ? QCoreApplication::translate("AIAssistant",
                                                                 "Could not read %1.").arg(skillId)
                                   : error)
                : QString();
        const QJsonObject document =
            diagnostic.isEmpty() ? QJsonDocument::fromJson(output).object() : QJsonObject();
        const QPointer<AIAssistant> assistant(this);
        QTimer::singleShot(0, owner, [assistant, owner, skillId, reload, diagnostic, document]() {
          if(!assistant || !owner)
            return;
          if(!diagnostic.isEmpty())
          {
            RDDialog::critical(owner, QCoreApplication::translate("AIAssistant", "Skill"),
                               diagnostic);
            return;
          }
          assistant->presentSkillDocument(owner, skillId, document, reload);
        });
      });
}

void AIAssistant::presentSkillDocument(QWidget *parent, const QString &skillId,
                                       const QJsonObject &document,
                                       const std::function<void()> &reload)
{
  const QPointer<QWidget> owner(parent);
  QDialog dialog(parent);
  dialog.setWindowTitle(tr("Skill: %1").arg(skillId));
  dialog.resize(760, 620);
  QVBoxLayout *dialogLayout = new QVBoxLayout(&dialog);
  QLabel *where = new QLabel(
      document.value(lit("copyOnWrite")).toBool()    // i18n-ignore: AgentHost protocol identifier, not UI prose.
          ? tr("Installed with RenderDoc (%1). Saving writes your copy to %2, which is loaded "
               "instead from then on and survives the next update.")
                .arg(document.value(lit("path")).toString(),    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                     document.value(lit("writePath")).toString())
          : tr("Your Skill: %1").arg(document.value(lit("path")).toString()),    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      &dialog);
  where->setWordWrap(true);
  dialogLayout->addWidget(where);
  QPlainTextEdit *editor = new QPlainTextEdit(&dialog);
  editor->setObjectName(lit("AISkillEditor"));    // i18n-ignore: automation id.
  editor->setPlainText(document.value(lit("content")).toString());
  editor->setLineWrapMode(QPlainTextEdit::NoWrap);
  editor->setFont(Formatter::FixedFont());
  dialogLayout->addWidget(editor, 1);
  QDialogButtonBox *buttons =
      new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel, &dialog);
  dialogLayout->addWidget(buttons);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  if(RDDialog::show(&dialog) != QDialog::Accepted)
    return;
  const QString edited = editor->toPlainText();
  if(edited == document.value(lit("content")).toString())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    return;
  runCLIAsync(
      // i18n-ignore: Fixed AgentHost CLI command and option identifiers.
      {lit("tools"), lit("skill-write"), skillId, lit("--content-stdin"), lit("--json")},
      edited.toUtf8(), 30000, parent,
      [owner, reload, skillId](int writeExit, const QByteArray &, const QString &writeError,
                               bool writeUnavailable) {
        if(!owner)
          return;
        if(writeExit != 0 || writeUnavailable)
        {
          const QString failure =
              writeError.isEmpty()
                  ? QCoreApplication::translate("AIAssistant", "Could not save %1.").arg(skillId)
                  : writeError;
          QTimer::singleShot(0, owner, [owner, failure]() {
            if(owner)
              RDDialog::critical(owner, QCoreApplication::translate("AIAssistant", "Skill"),
                                 failure);
          });
          return;
        }
        if(reload)
          reload();
      });
}

void AIAssistant::editToolProvider(QWidget *parent, const QJsonObject &existing,
                                   ToolDialogMode mode)
{
  // One dialog for every kind of tool, and it asks for one thing: where it is. An MCP
  // server describes itself over tools/list; a command line is worked out against real
  // assets. Neither is something a person should have to write down.
  const QString existingId = existing.value(lit("id")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  // A seeded connection kind (Add from the Unpackers tab) carries no id, so this is
  // still a creation and the kind stays editable.
  const bool creating = existingId.isEmpty();
  const QJsonObject connection = existing.value(lit("connection")).toObject();    // i18n-ignore: AgentHost protocol identifier, not UI prose.

  // The MCP tab and the Unpackers tab each open this on their own kind: a server
  // dialog never offers "command line program" and a program dialog never offers a
  // transport, so the form only ever shows the fields that apply.
  const QString currentKind = connection.value(lit("kind")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  if(mode == ToolDialogMode::Any)
    mode = currentKind == lit("cli") ? ToolDialogMode::Program : ToolDialogMode::Server;    // i18n-ignore: protocol value.
  const bool serverDialog = mode == ToolDialogMode::Server;

  QDialog dialog(parent);
  dialog.setWindowTitle(serverDialog ? (creating ? tr("Add MCP server") : tr("Edit MCP server"))
                                     : (creating ? tr("Add custom unpacker program")
                                                 : tr("Edit unpacker program")));
  dialog.setMinimumWidth(660);
  QVBoxLayout *dialogLayout = new QVBoxLayout(&dialog);
  QFormLayout *form = new QFormLayout();
  dialogLayout->addLayout(form);

  QComboBox *kind = new QComboBox(&dialog);
  if(serverDialog)
  {
    kind->addItem(tr("Program on this machine (stdio)"), lit("mcp-stdio"));    // i18n-ignore: protocol value.
    kind->addItem(tr("Streamable HTTP"), lit("mcp-http"));    // i18n-ignore: protocol value.
  }
  else
  {
    kind->addItem(tr("Command line program"), lit("cli"));    // i18n-ignore: protocol value.
  }
  kind->setCurrentIndex(qMax(0, kind->findData(currentKind.isEmpty() ? kind->itemData(0).toString()
                                                                     : currentKind)));
  kind->setEnabled(creating && serverDialog);
  QLineEdit *title = new QLineEdit(existing.value(lit("title")).toString(), &dialog);    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  title->setPlaceholderText(tr("Optional; the program or host name is used otherwise"));
  QLineEdit *description = new QLineEdit(existing.value(lit("description")).toString(), &dialog);    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  description->setPlaceholderText(tr("What it is for; Bread AI reads this when choosing"));
  if(serverDialog)
    form->addRow(tr("Transport"), kind);
  else
    kind->hide();    // not in the layout, so it must not paint at the dialog origin
  form->addRow(tr("Name"), title);
  form->addRow(tr("Description"), description);

  QWidget *programRow = new QWidget(&dialog);
  QHBoxLayout *programLayout = new QHBoxLayout(programRow);
  programLayout->setContentsMargins(0, 0, 0, 0);
  QLineEdit *command = new QLineEdit(connection.value(lit("command")).toString(), programRow);    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  command->setPlaceholderText(tr("For example C:\\tools\\uex\\uex.exe"));
  QPushButton *browse = new QPushButton(tr("Browse..."), programRow);
  programLayout->addWidget(command, 1);
  programLayout->addWidget(browse);
  QPlainTextEdit *arguments = new QPlainTextEdit(&dialog);
  arguments->setMaximumHeight(64);
  QStringList existingArguments;
  for(const QJsonValue &value : connection.value(lit("args")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    existingArguments << value.toString();
  arguments->setPlainText(existingArguments.join(QLatin1Char('\n')));
  QPlainTextEdit *environment = new QPlainTextEdit(&dialog);
  environment->setMaximumHeight(56);
  QStringList existingEnvironment;
  const QJsonObject env = connection.value(lit("env")).toObject();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  for(auto it = env.begin(); it != env.end(); ++it)
    existingEnvironment << lit("%1=%2").arg(it.key(), it.value().toString());
  environment->setPlainText(existingEnvironment.join(QLatin1Char('\n')));

  QLineEdit *url = new QLineEdit(connection.value(lit("url")).toString(), &dialog);    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  url->setPlaceholderText(lit("https://mcp.example.com/mcp"));    // i18n-ignore: URL example.
  QLineEdit *bearer = new QLineEdit(connection.value(lit("bearerEnv")).toString(), &dialog);    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  bearer->setPlaceholderText(lit("MCP_BEARER_TOKEN"));    // i18n-ignore: environment variable example.
  QPlainTextEdit *headers = new QPlainTextEdit(&dialog);
  headers->setMaximumHeight(56);
  QStringList existingHeaders;
  const QJsonObject headerValues = connection.value(lit("headers")).toObject();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  for(auto it = headerValues.begin(); it != headerValues.end(); ++it)
    existingHeaders << lit("%1=%2").arg(it.key(), it.value().toString());
  headers->setPlainText(existingHeaders.join(QLatin1Char('\n')));

  QLabel *programLabel = new QLabel(tr("Program"), &dialog);
  QLabel *argumentsLabel = new QLabel(tr("Arguments (one per line)"), &dialog);
  QLabel *environmentLabel = new QLabel(tr("Environment, NAME=value (optional)"), &dialog);
  QLabel *urlLabel = new QLabel(tr("URL"), &dialog);
  QLabel *bearerLabel = new QLabel(tr("Bearer token environment variable"), &dialog);
  QLabel *headersLabel = new QLabel(tr("Headers, Name=value (optional)"), &dialog);
  form->addRow(programLabel, programRow);
  form->addRow(argumentsLabel, arguments);
  form->addRow(environmentLabel, environment);
  form->addRow(urlLabel, url);
  form->addRow(bearerLabel, bearer);
  form->addRow(headersLabel, headers);

  QGroupBox *roles = new QGroupBox(tr("Used for"), &dialog);
  QVBoxLayout *roleLayout = new QVBoxLayout(roles);
  QCheckBox *generalRole = new QCheckBox(tr("AI tasks - its operations are offered during a task"),
                                         roles);
  QCheckBox *readerRole = new QCheckBox(
      tr("Reading assets - RenderDoc calls it while identifying unpacked files"), roles);
  roleLayout->addWidget(generalRole);
  roleLayout->addWidget(readerRole);
  dialogLayout->addWidget(roles);
  const QJsonArray existingRoles = existing.value(lit("roles")).toArray();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  // For a new tool the sensible roles follow the connection kind, and the kind is
  // chosen after this dialog is built -- so the defaults are applied by showFieldsFor
  // below. Deciding here used the kind of the tool being *edited*, which is empty when
  // adding, and left a command-line extractor with only the "AI tasks" role: a
  // combination nothing reaches, so the tool was registered and then never used.
  if(!existingRoles.isEmpty())
  {
    for(const QJsonValue &value : existingRoles)
    {
      if(value.toString() == lit("general"))    // i18n-ignore: protocol value.
        generalRole->setChecked(true);
      if(value.toString() == lit("asset-reader"))    // i18n-ignore: protocol value.
        readerRole->setChecked(true);
    }
  }

  QLabel *explanation = new QLabel(&dialog);
  explanation->setWordWrap(true);
  dialogLayout->addWidget(explanation);

  QDialogButtonBox *buttons =
      new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
  dialogLayout->addWidget(buttons);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  connectAIConfigurationSignal(browse, &QPushButton::clicked, &dialog, [&dialog, command]() {
    const QString chosen = RDDialog::getExecutableFileName(
        &dialog, QCoreApplication::translate("AIAssistant", "Choose the program"));
    if(!chosen.isEmpty())
      command->setText(chosen);
  });

  const auto showFieldsFor = [=](int) {
    const QString selected = kind->currentData().toString();
    const bool http = selected == lit("mcp-http");    // i18n-ignore: protocol value.
    const bool stdio = selected == lit("mcp-stdio");    // i18n-ignore: protocol value.
    if(existingRoles.isEmpty())
    {
      // A bare extractor is what RenderDoc calls while identifying assets; a server
      // describes its own operations and is offered during a task.
      const bool bare = selected == lit("cli");    // i18n-ignore: protocol value.
      generalRole->setChecked(!bare);
      readerRole->setChecked(bare);
    }
    for(QWidget *widget : {static_cast<QWidget *>(programLabel), static_cast<QWidget *>(programRow),
                           static_cast<QWidget *>(argumentsLabel),
                           static_cast<QWidget *>(arguments),
                           static_cast<QWidget *>(environmentLabel),
                           static_cast<QWidget *>(environment)})
      widget->setVisible(!http);
    for(QWidget *widget : {static_cast<QWidget *>(urlLabel), static_cast<QWidget *>(url),
                           static_cast<QWidget *>(bearerLabel), static_cast<QWidget *>(bearer),
                           static_cast<QWidget *>(headersLabel), static_cast<QWidget *>(headers)})
      widget->setVisible(http);
    // Arguments only make sense for a server that is started; a bare extractor is
    // driven by the invocation Bread AI works out, not by one typed here.
    argumentsLabel->setVisible(stdio);
    arguments->setVisible(stdio);
    environmentLabel->setVisible(stdio);
    environment->setVisible(stdio);
    explanation->setText(
        http ? QCoreApplication::translate(
                   "AIAssistant",
                   "The token is read from that environment variable when the server is called, "
                   "so it is never written into the configuration file. Plain http:// is "
                   "accepted only for localhost.")
             : (stdio ? QCoreApplication::translate(
                            "AIAssistant",
                            "The server is started on this machine with your account and "
                            "Bread AI may call its operations during a task, so only add "
                            "servers you trust. It describes itself, so there is nothing else to "
                            "configure.")
                      : QCoreApplication::translate(
                            "AIAssistant",
                            "That is all that is needed. Bread AI runs the program against "
                            "real files in your project to work out how to call it, and only "
                            "uses an invocation it watched succeed.")));
  };
  connectAIConfigurationSignal(kind, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged),
                               &dialog, showFieldsFor);
  showFieldsFor(0);

  if(RDDialog::show(&dialog) != QDialog::Accepted)
    return;

  const QString selected = kind->currentData().toString();
  const bool http = selected == lit("mcp-http");    // i18n-ignore: protocol value.
  if(http ? url->text().trimmed().isEmpty() : command->text().trimmed().isEmpty())
  {
    RDDialog::critical(parent, serverDialog ? tr("Add MCP server") : tr("Add unpacker program"),
                       http ? tr("A URL is required.") : tr("A program path is required."));
    return;
  }

  const auto pairs = [](const QString &text) {
    QJsonObject values;
    for(const QString &line : text.split(QLatin1Char('\n')))
    {
      const int separator = line.indexOf(QLatin1Char('='));
      if(separator <= 0)
        continue;
      values[line.left(separator).trimmed()] = line.mid(separator + 1).trimmed();
    }
    return values;
  };

  QJsonObject definition;
  QJsonObject newConnection;
  // Start from what is already stored so a field this dialog does not show -- a
  // header taken from an environment variable, say -- survives pressing OK. Rebuilding
  // the connection from the visible fields alone silently dropped it, and the tool
  // then looked like a different program, which threw away every recipe discovered
  // for it. Only when the kind itself changes is the old connection irrelevant.
  const QJsonObject previousConnection = existing.value(lit("connection")).toObject();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  if(previousConnection.value(lit("kind")).toString() == selected)    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    newConnection = previousConnection;
  // i18n-ignore: AgentHost tool-configuration protocol field names.
  newConnection[lit("kind")] = selected;
  if(http)
  {
    newConnection[lit("url")] = url->text().trimmed();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    newConnection[lit("bearerEnv")] = bearer->text().trimmed();
    newConnection[lit("headers")] = pairs(headers->toPlainText());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  }
  else
  {
    newConnection[lit("command")] = command->text().trimmed();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    QJsonArray argumentValues;
    if(selected == lit("mcp-stdio"))    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      for(const QString &line : arguments->toPlainText().split(QLatin1Char('\n')))
        if(!line.trimmed().isEmpty())
          argumentValues.append(line.trimmed());
    // Only an MCP server has editable args and env in this dialog. Writing empty ones
    // for a bare command-line tool would change its connection identity, and every
    // invocation Bread AI worked out for it would be discarded as belonging to a
    // different program.
    if(selected == lit("mcp-stdio"))    // i18n-ignore: protocol value.
    {
      newConnection[lit("args")] = argumentValues;    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      newConnection[lit("env")] = pairs(environment->toPlainText());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    }
    else if(!newConnection.contains(lit("args")))    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    {
      newConnection[lit("args")] = QJsonArray();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    }
  }
  const QString display = title->text().trimmed().isEmpty()
                              ? (http ? QUrl(url->text().trimmed()).host()
                                      : QFileInfo(command->text().trimmed()).completeBaseName())
                              : title->text().trimmed();
  definition[lit("id")] = existingId.isEmpty() ? stableConfigurationId(display) : existingId;    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  definition[lit("title")] = display.isEmpty() ? definition[lit("id")].toString() : display;
  definition[lit("description")] = description->text().trimmed();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  definition[lit("enabled")] = existing.value(lit("enabled")).toBool(true);
  QJsonArray roleValues;
  if(generalRole->isChecked())
    roleValues.append(lit("general"));    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  if(readerRole->isChecked())
    roleValues.append(lit("asset-reader"));    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  if(roleValues.isEmpty())
    roleValues.append(lit("general"));    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  definition[lit("roles")] = roleValues;
  definition[lit("connection")] = newConnection;    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  // The interface is not carried here: upsert keeps the discovered one when the
  // connection is unchanged, and drops it when it is not.
  runConfigurationEdit(parent,
                       {lit("tools"), lit("tool-set"), lit("--json-stdin"), lit("--json")},    // i18n-ignore: CLI command
                       QJsonDocument(definition).toJson(QJsonDocument::Compact));
}

void AIAssistant::editBuiltinUnpacker(QWidget *parent, const QJsonObject &unpacker,
                                      const QString &detectedProgram)
{
  // One dialog per shipped unpacker. The command line, the formats it reads and the
  // operations offered to the AI are fixed by the recipe; the person supplies the
  // program and, for uex, which profile file and profile name to mount.
  const QString kind = unpacker.value(lit("kind")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  const bool configured = unpacker.value(lit("configured")).toBool();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  const QJsonObject values = unpacker.value(lit("values")).toObject();    // i18n-ignore: AgentHost protocol identifier, not UI prose.

  QDialog dialog(parent);
  dialog.setWindowTitle(unpacker.value(lit("title")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  dialog.setMinimumWidth(680);
  QVBoxLayout *dialogLayout = new QVBoxLayout(&dialog);

  QLabel *reads = new QLabel(unpacker.value(lit("reads")).toString(), &dialog);    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  reads->setWordWrap(true);
  dialogLayout->addWidget(reads);
  QStringList formats;
  for(const QJsonValue &pattern : unpacker.value(lit("patterns")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    formats << pattern.toString();
  QLabel *formatLabel = new QLabel(tr("Reads: %1").arg(formats.join(QLatin1Char(' '))), &dialog);
  formatLabel->setWordWrap(true);
  formatLabel->setStyleSheet(lit("color: #9a9a9a;"));    // i18n-ignore: stylesheet.
  dialogLayout->addWidget(formatLabel);
  const QString homepage = unpacker.value(lit("homepage")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  if(!homepage.isEmpty())
  {
    QLabel *link = new QLabel(lit("<a href=\"%1\">%1</a>").arg(homepage.toHtmlEscaped()), &dialog);    // i18n-ignore: HTML link markup.
    link->setOpenExternalLinks(true);
    dialogLayout->addWidget(link);
  }
  // The program itself is third-party and not shipped. Where to put it so that
  // "Find" sees it is spelled out, with the folder one click away.
  if(kind != lit("unitypy"))    // i18n-ignore: built-in unpacker kind identifier.
  {
    const QString dropFolder =
        QDir::toNativeSeparators(QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation) +
                                 lit("/ReplayLab/tools/unpackers/") + kind);    // i18n-ignore: fixed data folder path.
    QWidget *dropRow = new QWidget(&dialog);
    QHBoxLayout *dropLayout = new QHBoxLayout(dropRow);
    dropLayout->setContentsMargins(0, 0, 0, 0);
    QLabel *dropHint = new QLabel(
        tr("Not installed yet? Download a release from the page above and unzip it flat into %1 "
           "-- Find picks it up from there.")
            .arg(dropFolder),
        dropRow);
    dropHint->setWordWrap(true);
    dropHint->setStyleSheet(lit("color: #9a9a9a;"));    // i18n-ignore: stylesheet.
    QPushButton *openDrop = new QPushButton(tr("Open Folder"), dropRow);
    dropLayout->addWidget(dropHint, 1);
    dropLayout->addWidget(openDrop, 0, Qt::AlignTop);
    dialogLayout->addWidget(dropRow);
    connectAIConfigurationSignal(openDrop, &QPushButton::clicked, &dialog, [dropFolder]() {
      QDir().mkpath(dropFolder);
      QDesktopServices::openUrl(QUrl::fromLocalFile(dropFolder));
    });
  }
  else
  {
    QLabel *pipHint = new QLabel(
        tr("The reader script ships with Bread AI; the interpreter you pick only needs the UnityPy "
           "package (pip install UnityPy)."),
        &dialog);
    pipHint->setWordWrap(true);
    pipHint->setStyleSheet(lit("color: #9a9a9a;"));    // i18n-ignore: stylesheet.
    dialogLayout->addWidget(pipHint);
  }

  QFormLayout *form = new QFormLayout();
  dialogLayout->addLayout(form);
  QWidget *programRow = new QWidget(&dialog);
  QHBoxLayout *programLayout = new QHBoxLayout(programRow);
  programLayout->setContentsMargins(0, 0, 0, 0);
  QLineEdit *program = new QLineEdit(
      detectedProgram.isEmpty() ? unpacker.value(lit("program")).toString() : detectedProgram,    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      programRow);
  program->setPlaceholderText(unpacker.value(lit("programHint")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  QPushButton *browse = new QPushButton(tr("Browse..."), programRow);
  QPushButton *find = new QPushButton(tr("Find"), programRow);
  find->setToolTip(tr("Look on PATH and in the usual tool folders for this program."));
  programLayout->addWidget(program, 1);
  programLayout->addWidget(browse);
  programLayout->addWidget(find);
  form->addRow(tr("Program"), programRow);

  // Option fields come from the recipe: a file, a line of text or a switch.
  QHash<QString, QLineEdit *> textOptions;
  QHash<QString, QCheckBox *> boolOptions;
  for(const QJsonValue &value : unpacker.value(lit("options")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
  {
    const QJsonObject option = value.toObject();
    const QString key = option.value(lit("key")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    const QString optionKind = option.value(lit("kind")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    const QString label = option.value(lit("title")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    const QString hint = option.value(lit("hint")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    if(optionKind == lit("bool"))    // i18n-ignore: protocol value.
    {
      QCheckBox *box = new QCheckBox(label, &dialog);
      box->setToolTip(hint);
      box->setChecked(values.contains(key) ? values.value(key).toBool()
                                           : option.value(lit("default")).toBool(true));    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      form->addRow(QString(), box);
      boolOptions[key] = box;
      continue;
    }
    QWidget *row = new QWidget(&dialog);
    QHBoxLayout *rowLayout = new QHBoxLayout(row);
    rowLayout->setContentsMargins(0, 0, 0, 0);
    QLineEdit *edit = new QLineEdit(values.value(key).toString(), row);
    edit->setPlaceholderText(hint);
    edit->setToolTip(hint);
    rowLayout->addWidget(edit, 1);
    if(optionKind == lit("file"))    // i18n-ignore: protocol value.
    {
      QPushButton *pick = new QPushButton(tr("Browse..."), row);
      rowLayout->addWidget(pick);
      connectAIConfigurationSignal(pick, &QPushButton::clicked, &dialog, [&dialog, edit, label]() {
        const QString chosen = RDDialog::getOpenFileName(&dialog, label);
        if(!chosen.isEmpty())
          edit->setText(chosen);
      });
    }
    form->addRow(label, row);
    textOptions[key] = edit;
  }
  QCheckBox *offer = new QCheckBox(tr("Offer its operations to Bread AI during tasks"), &dialog);
  offer->setChecked(true);
  dialogLayout->addWidget(offer);
  QLabel *status = new QLabel(&dialog);
  status->setWordWrap(true);
  dialogLayout->addWidget(status);

  QDialogButtonBox *buttons =
      new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
  dialogLayout->addWidget(buttons);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  connectAIConfigurationSignal(browse, &QPushButton::clicked, &dialog, [&dialog, program]() {
    const QString chosen = RDDialog::getExecutableFileName(
        &dialog, QCoreApplication::translate("AIAssistant", "Choose the program"));
    if(!chosen.isEmpty())
      program->setText(chosen);
  });
  const QPointer<QDialog> dialogPointer(&dialog);
  connectAIConfigurationSignal(find, &QPushButton::clicked, &dialog, [this, dialogPointer, program,
                                                                     status, kind, find]() {
    if(!dialogPointer)
      return;
    find->setEnabled(false);
    setDiagnosticStatus(status, DiagnosticLevel::Info,
                        QCoreApplication::translate("AIAssistant", "Looking..."));
    runCLIAsync(
        // i18n-ignore: Fixed AgentHost CLI command and option identifiers.
        {lit("tools"), lit("tool-detect"), kind, lit("--json")}, QByteArray(), 45000,
        dialogPointer,
        [dialogPointer, program, status, find](int exitCode, const QByteArray &output,
                                               const QString &error, bool unavailable) {
          if(!dialogPointer)
            return;
          find->setEnabled(true);
          if(exitCode != 0 || unavailable)
          {
            setDiagnosticStatus(status, DiagnosticLevel::Error,
                                error.isEmpty() ? QCoreApplication::translate(
                                                      "AIAssistant", "The search failed.")
                                                : error);
            return;
          }
          const QJsonArray candidates =
              QJsonDocument::fromJson(output).object().value(lit("candidates")).toArray();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
          if(candidates.isEmpty())
          {
            setDiagnosticStatus(status, DiagnosticLevel::Warning,
                                QCoreApplication::translate(
                                    "AIAssistant", "Not found in the usual places; use Browse."));
            return;
          }
          const QJsonObject first = candidates.first().toObject();
          program->setText(first.value(lit("command")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
          QStringList others;
          for(int index = 1; index < candidates.size(); ++index)
            others << candidates.at(index).toObject().value(lit("command")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
          const QString note = first.value(lit("description")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
          setDiagnosticStatus(
              status, DiagnosticLevel::Success,
              others.isEmpty()
                  ? note
                  : QCoreApplication::translate("AIAssistant", "%1 (also found: %2)")
                        .arg(note, others.join(lit(", "))));
        });
  });

  if(RDDialog::show(&dialog) != QDialog::Accepted)
    return;
  if(program->text().trimmed().isEmpty())
  {
    RDDialog::critical(parent, unpacker.value(lit("title")).toString(),    // i18n-ignore: AgentHost protocol identifier, not UI prose.
                       tr("A program path is required."));
    return;
  }
  QJsonObject options;
  for(auto it = textOptions.begin(); it != textOptions.end(); ++it)
    options[it.key()] = it.value()->text().trimmed();
  for(auto it = boolOptions.begin(); it != boolOptions.end(); ++it)
    options[it.key()] = it.value()->isChecked();
  QJsonObject definition;
  // i18n-ignore: AgentHost tool-configuration protocol field names.
  definition[lit("builtinKind")] = kind;
  definition[lit("program")] = program->text().trimmed(); // i18n-ignore: tool configuration JSON field.
  definition[lit("builtinOptions")] = options; // i18n-ignore: tool configuration JSON field.
  definition[lit("offerToAgent")] = offer->isChecked(); // i18n-ignore: tool configuration JSON field.
  definition[lit("enabled")] = configured ? unpacker.value(lit("enabled")).toBool(true) : true; // i18n-ignore: tool configuration JSON fields.
  runConfigurationEdit(parent,
                       {lit("tools"), lit("tool-set"), lit("--json-stdin"), lit("--json")},    // i18n-ignore: CLI command
                       QJsonDocument(definition).toJson(QJsonDocument::Compact));
}

void AIAssistant::detectAssetReaders(QWidget *parent)
{
  // "Configure some of the ones I already have" -- look where these tools normally live
  // and offer what is actually on this machine.
  const QPointer<QWidget> owner(parent);
  runCLIAsync(
      // i18n-ignore: Fixed AgentHost CLI command and option identifiers.
      {lit("tools"), lit("reader-discover"), lit("--json")}, QByteArray(), 30000, parent,
      [this, owner](int exitCode, const QByteArray &output, const QString &error,
                    bool unavailable) {
        if(!owner)
          return;
        const QString diagnostic =
            (exitCode != 0 || unavailable)
                ? (error.isEmpty() ? QCoreApplication::translate(
                                         "AIAssistant", "Could not look for installed extractors.")
                                   : error)
                : QString();
        const QJsonArray candidates =
            QJsonDocument::fromJson(output).object().value(lit("candidates")).toArray();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
        const QPointer<AIAssistant> assistant(this);
        // Never open a modal window inside the command runner's own callback.
        QTimer::singleShot(0, owner, [assistant, owner, diagnostic, candidates]() {
          if(!assistant || !owner)
            return;
          if(!diagnostic.isEmpty())
          {
            RDDialog::critical(owner, QCoreApplication::translate("AIAssistant", "Asset readers"),
                               diagnostic);
            return;
          }
          assistant->presentDetectedAssetReaders(owner, candidates);
        });
      });
}

void AIAssistant::presentDetectedAssetReaders(QWidget *parent, const QJsonArray &candidates)
{
  if(candidates.isEmpty())
  {
    RDDialog::information(
        parent, tr("Asset readers"),
        tr("No known extractor was found in the usual places. Add one with its path, or ask "
           "Bread AI to set it up for you."));
    return;
  }

  QDialog dialog(parent);
  dialog.setWindowTitle(tr("Installed extractors"));
  dialog.setMinimumWidth(680);
  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QLabel *intro = new QLabel(tr("Tick the ones to register. Nothing is changed until you accept."),
                             &dialog);
  intro->setWordWrap(true);
  layout->addWidget(intro);
  QTreeWidget *tree = new QTreeWidget(&dialog);
  tree->setColumnCount(3);
  tree->setHeaderLabels({tr("Name"), tr("Program"), tr("What it reads")});
  tree->setRootIsDecorated(false);
  tree->header()->setSectionResizeMode(2, QHeaderView::Stretch);
  for(const QJsonValue &value : candidates)
  {
    const QJsonObject candidate = value.toObject();
    const bool already = candidate.value(lit("alreadyRegistered")).toBool();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    QTreeWidgetItem *item = new QTreeWidgetItem(tree);
    item->setText(0, candidate.value(lit("title")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    item->setText(1, candidate.value(lit("command")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    item->setText(2, already ? QCoreApplication::translate("AIAssistant", "Already registered")
                             : candidate.value(lit("description")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    item->setData(0, Qt::UserRole, candidate.value(lit("command")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    item->setData(0, Qt::UserRole + 1, candidate.value(lit("description")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    item->setData(0, Qt::UserRole + 2, candidate.value(lit("id")).toString());    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    // A candidate that is one of the shipped unpackers opens its own dialog below,
    // pre-filled with the program that was found; anything else is registered as
    // a custom program.
    item->setFlags(already ? (item->flags() & ~Qt::ItemIsUserCheckable & ~Qt::ItemIsEnabled)
                           : (item->flags() | Qt::ItemIsUserCheckable));
    item->setCheckState(0, already ? Qt::Unchecked : Qt::Checked);
  }
  layout->addWidget(tree, 1);
  QDialogButtonBox *buttons =
      new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
  layout->addWidget(buttons);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  if(RDDialog::show(&dialog) != QDialog::Accepted)
    return;

  const QJsonArray builtins = builtinUnpackerCatalog();
  for(int index = 0; index < tree->topLevelItemCount(); ++index)
  {
    QTreeWidgetItem *item = tree->topLevelItem(index);
    if(item->checkState(0) != Qt::Checked)
      continue;
    const QString command = item->data(0, Qt::UserRole).toString();
    const QString presetKind = item->data(0, Qt::UserRole + 2).toString();
    bool handled = false;
    for(const QJsonValue &value : builtins)
      if(!presetKind.isEmpty() && value.toObject().value(lit("kind")).toString() == presetKind)    // i18n-ignore: AgentHost protocol identifier, not UI prose.
      {
        editBuiltinUnpacker(parent, value.toObject(), command);
        handled = true;
        break;
      }
    if(handled)
      continue;
    QStringList arguments = {lit("tools"), lit("tool-add"), lit("--kind"), lit("cli"),    // i18n-ignore: CLI command
                             lit("--command"), command, lit("--json")};
    const QString description = item->data(0, Qt::UserRole + 1).toString();
    if(!description.isEmpty())
      arguments << lit("--description") << description;    // i18n-ignore: CLI option
    runConfigurationEdit(parent, arguments, QByteArray());
  }
}

void AIAssistant::runConfigurationEdit(QWidget *parent, const QStringList &arguments,
                                       const QByteArray &input)
{
  const QPointer<QWidget> owner(parent);
  const QPointer<AIAssistant> assistant(this);
  runCLIAsync(arguments, input, 30000, parent,
              [assistant, owner](int exitCode, const QByteArray &, const QString &error,
                                 bool unavailable) {
                if(!assistant)
                  return;
                if((exitCode != 0 || unavailable) && owner)
                {
                  const QString failure =
                      error.isEmpty()
                          ? QCoreApplication::translate("AIAssistant", "The change was refused.")
                          : error;
                  QTimer::singleShot(0, owner, [owner, failure]() {
                    if(owner)
                      RDDialog::critical(
                          owner, QCoreApplication::translate("AIAssistant", "Settings"), failure);
                  });
                }
                if(assistant->m_ToolCatalogReload)
                  assistant->m_ToolCatalogReload();
              });
}

void AIAssistant::refreshToolMentionCatalog()
{
  const QPointer<AIAssistant> owner(this);
  runCLIAsync(
      // i18n-ignore: Fixed AgentHost CLI command and option identifiers.
      {lit("tools"), lit("list"), lit("--json")}, QByteArray(), 15000, this,
      [owner](int exitCode, const QByteArray &output, const QString &, bool unavailable) {
        if(!owner || exitCode != 0 || unavailable)
          return;
        const QJsonObject view = QJsonDocument::fromJson(output).object();
        QStringList skillIds;
        for(const QJsonValue &value : view.value(lit("skills")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
        {
          const QJsonObject skill = value.toObject();
          if(skill.value(lit("enabled")).toBool() && !skill.value(lit("always")).toBool())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            skillIds << skill.value(lit("id")).toString();
        }
        QStringList serverIds;
        for(const QJsonValue &value : view.value(lit("mcpServers")).toArray())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
        {
          const QJsonObject server = value.toObject();
          if(server.value(lit("enabled")).toBool())    // i18n-ignore: AgentHost protocol identifier, not UI prose.
            serverIds << server.value(lit("id")).toString();
        }
        owner->m_MentionSkillIds = skillIds;
        owner->m_MentionMcpIds = serverIds;
      });
}

QStringList AIAssistant::mentionedSkillIds(const QString &prompt) const
{
  QStringList found;
  // i18n-ignore: composer mention syntax.
  const QRegularExpression pattern(lit("@skill:([a-z0-9][a-z0-9._-]{0,63})"));
  QRegularExpressionMatchIterator it = pattern.globalMatch(prompt);
  while(it.hasNext())
  {
    const QString id = it.next().captured(1);
    if(!found.contains(id))
      found << id;
  }
  return found;
}

QStringList AIAssistant::mentionedMcpServerIds(const QString &prompt) const
{
  QStringList found;
  // i18n-ignore: composer mention syntax.
  const QRegularExpression pattern(lit("@mcp:([a-z0-9][a-z0-9._-]{0,63})"));
  QRegularExpressionMatchIterator it = pattern.globalMatch(prompt);
  while(it.hasNext())
  {
    const QString id = it.next().captured(1);
    if(!found.contains(id))
      found << id;
  }
  return found;
}

void AIAssistant::manageConnections()
{
  if(m_ConnectionDialogOpen)
    return;

  m_ConnectionDialogOpen = true;
  SettingsDialog settings(m_Ctx, this);
  settings.focusPage(lit("AI"));
  RDDialog::show(&settings);
  m_ConnectionDialogOpen = false;
  refreshProfiles();
  refreshConnectionStatus();
}

// Kept temporarily as a source-compatible fallback for downstream forks that still invoke the
// former one-shot editor. qrenderdoc itself now exposes only the reusable Settings > AI page.
void AIAssistant::runDoctor()
{
  const ConversationThread *thread = conversation();
  const QString profileId = thread ? thread->profileId : m_ActiveProfile;
  if(profileId.isEmpty())
  {
    appendSystem(tr("Add or select an AI connection first."), true);
    return;
  }
  runConnectionCheck(this, profileId, false);
}

void AIAssistant::refreshConnectionStatus()
{
  if(m_ConnectionStatusTimer)
    m_ConnectionStatusTimer->stop();
  if(m_ShuttingDown || m_ConnectionDialogOpen)
    return;
  if(m_CLIPath.isEmpty())
    m_CLIPath = findAgentCLI();
  if(m_CLIPath.isEmpty())
    setDiagnosticStatus(m_Status, DiagnosticLevel::Error, tr("Host missing"));
  else if(m_ActiveProfile.isEmpty())
    setDiagnosticStatus(m_Status, DiagnosticLevel::Warning, tr("Add connection"));
  else if(m_Ctx.IsCaptureLoaded() && findBridgeInstance().isEmpty())
  {
    setDiagnosticStatus(m_Status, DiagnosticLevel::Warning, tr("Waiting for bridge"));
    // Capture loading can precede the first bridge manifest. Retry this pending
    // state so the badge becomes ready without a model change or another send.
    if(m_ConnectionStatusTimer)
      m_ConnectionStatusTimer->start();
  }
  else
    setDiagnosticStatus(m_Status, DiagnosticLevel::Success, tr("Chat ready"));
}

void AIAssistant::cancelConfigurationProcesses()
{
  // Configuration commands are owned jointly by their Settings surface and this assistant. The
  // surface can outlive us, so revoke every completion and close each kill-on-close Job during
  // Shutdown. No wait is necessary: closing the Job terminates the contained process tree and the
  // QProcess object retires on the GUI event loop (or with its still-live owner).
  const QList<std::shared_ptr<AIConfigurationProcessState>> states =
      m_ConfigurationProcesses.values();
  m_ConfigurationProcesses.clear();
  for(const std::shared_ptr<AIConfigurationProcessState> &state : states)
  {
    if(!state)
      continue;
    if(!state->completed)
      state->log("cancelled");
    state->completed = true;
    state->completion = std::function<void(int, const QByteArray &, const QString &, bool)>();
    if(state->timeout)
      state->timeout->stop();
    QProcess *process = state->process.data();
    if(process)
      disconnect(process, NULL, NULL, NULL);
    state->closeJob();
    if(process)
    {
      if(process->state() != QProcess::NotRunning)
        process->kill();
      process->deleteLater();
    }
  }
}

void AIAssistant::forceStopProcessesForDestruction()
{
  auto stopContainedProcess = [](QProcess *process) {
    if(!process)
      return;
    if(process->state() != QProcess::NotRunning)
    {
      process->kill();
      process->waitForFinished(1000);
    }
    if(process->state() == QProcess::NotRunning)
      delete process;
    else
    {
      // The Job has already issued a tree-wide kill. Keep a rare, slow-to-signal QProcess out of
      // the panel's QObject destructor and delete it when Windows reports the terminated parent.
      process->setParent(QCoreApplication::instance());
      deleteProcessWhenFinished(process);
    }
  };

  const QList<ChatExecution *> executions = m_ChatExecutions.values();
  m_ChatExecutions.clear();
  for(ChatExecution *execution : executions)
  {
    if(!execution)
      continue;
    if(execution->startTimer)
      execution->startTimer->stop();
    execution->pendingInput.clear();
    execution->promptSent = false;
    QProcess *chatProcess = execution->process;
    execution->process = NULL;
    requestChatCancellation(execution);
    if(!chatProcess)
    {
      closeKillOnCloseJob(execution->jobHandle);
      clearChatContextFile(execution);
      clearChatCancelFile(execution);
      delete execution->startTimer;
      execution->startTimer = NULL;
      delete execution;
      continue;
    }
    disconnect(chatProcess, NULL, this, NULL);
    if(chatProcess->state() != QProcess::NotRunning)
      chatProcess->waitForFinished(2500);
    closeKillOnCloseJob(execution->jobHandle);
    stopContainedProcess(chatProcess);
    clearChatContextFile(execution);
    clearChatCancelFile(execution);
    delete execution->startTimer;
    execution->startTimer = NULL;
    delete execution;
  }
}

QString AIAssistant::newTaskId() const
{
  QString value = QUuid::createUuid().toString();
  value.remove(QLatin1Char('{'));
  value.remove(QLatin1Char('}'));
  value.remove(QLatin1Char('-'));
  return value;
}

QString AIAssistant::newMessageId() const
{
  return lit("message-") + newTaskId();
}

QString AIAssistant::conversationPrompt(const QString &prompt, const QString &conversationId,
                                        const QStringList &excludeMessageIds,
                                        const ClarificationResponse &clarificationResponse,
                                        const QVector<AIChatAttachment> &attachments,
                                        const QString &profileId) const
{
  const ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return prompt;
  QVector<const AIChatMessage *> history;
  int omittedHistoryCount = qBound(0, thread->omittedMessageCount, 1000000);
  for(const AIChatMessage &item : thread->messages)
  {
    if(excludeMessageIds.contains(item.id) || item.role == lit("system"))
      continue;
    if(item.role == lit("user") &&
       (item.state == lit("queued") || item.state == lit("running") ||
        item.state == lit("planning") || item.state == lit("finalizing")))
      continue;
    // i18n-ignore: Conversation roles are persisted protocol identifiers.
    if(item.role == lit("user") || item.role == lit("assistant"))
      history.push_back(&item);
  }
  // Persisted text/attachments are allocated newest-first. If the oldest retained boundary row is
  // incomplete, keep only the exact suffix after the last such row and account for the complete
  // unavailable prefix. This preserves ContextSummary lineage across a qrenderdoc restart.
  int incompleteBoundary = -1;
  for(int i = 0; i < history.size(); ++i)
    if(thread->incompleteHistoryMessageIds.contains(history[i]->id))
      incompleteBoundary = i;
  if(incompleteBoundary >= 0)
  {
    omittedHistoryCount = qMin(1000000, omittedHistoryCount + incompleteBoundary + 1);
    history.remove(0, incompleteBoundary + 1);
  }

  const QString effectiveProfile = profileId.isEmpty() ? m_ActiveProfile : profileId;
  const QStringList inputSchemas = m_ProfileRunInputSchemas.value(effectiveProfile);
  const bool supportsV5 =
      inputSchemas.contains(lit("replaylab-conversation/v5"));    // i18n-ignore: wire schema ID
  const bool supportsV4 =
      inputSchemas.contains(lit("replaylab-conversation/v4"));    // i18n-ignore: wire schema ID
  const bool supportsV3 =
      inputSchemas.contains(lit("replaylab-conversation/v3"));    // i18n-ignore: wire schema ID
  const bool supportsV2 =
      inputSchemas.contains(lit("replaylab-conversation/v2"));    // i18n-ignore: wire schema ID

  // A separately installed older AgentHost may not understand typed conversation envelopes.
  // Negotiate the input schema through `profile list`; otherwise use
  // the exact legacy English protocol (never a translated protocol literal).
  // i18n-ignore: versioned AgentHost wire-protocol identifier.
  if(!supportsV5 && !supportsV4 && !supportsV3 && !supportsV2)
  {
    auto legacyPrefix = [](int omitted) {
      // i18n-ignore: exact legacy AgentHost wire-protocol prefix; translating it breaks routing.
      QString value = lit("Continue this Bread AI conversation. Treat the final USER block as "
                          "the new request; earlier blocks are conversation history, not tool "
                          "evidence.\n\n");
      if(omitted > 0)
      {
        // i18n-ignore: legacy AgentHost protocol diagnostic. This is model input, not UI text.
        value += lit("HISTORY GAP: %1 older conversation message record(s) are unavailable to this "
                     "request. Preserve known conclusions from the supplied history, but do not "
                     "invent the missing contents or assume that this suffix is the full "
                     "conversation.\n\n")
                     .arg(omitted);
      }
      return value;
    };
    // A delimiter inside the current request is ambiguous in v1. Preserve the
    // exact request as ordinary input instead of misrouting a fabricated final block.
    if(prompt.contains(lit("\n\nUSER:\n")))    // i18n-ignore: legacy protocol delimiter
      return prompt;
    const QString current = lit("USER:\n") + prompt;    // i18n-ignore: legacy role marker
    QString rendered = legacyPrefix(qMin(1000000, omittedHistoryCount + history.size())) + current;
    QStringList blocks;
    for(int i = history.size() - 1; i >= 0; --i)
    {
      const AIChatMessage *item = history[i];
      // i18n-ignore: legacy AgentHost protocol role markers.
      const QString marker = item->role == lit("user") ? lit("USER:\n") : lit("ASSISTANT:\n");
      blocks.prepend(marker + item->text);
      const QString candidate =
          legacyPrefix(qMin(1000000, omittedHistoryCount + i)) +
          blocks.join(lit("\n\n")) + lit("\n\n") + current;
      if(candidate.toUtf8().size() > MaxPromptBytes)
      {
        blocks.removeFirst();
        rendered = legacyPrefix(qMin(1000000, omittedHistoryCount + i + 1)) +
                   (blocks.isEmpty() ? QString()
                                     : blocks.join(lit("\n\n")) + lit("\n\n")) +
                   current;
        break;
      }
      rendered = candidate;
    }
    return rendered;
  }

  QJsonObject request;
  // i18n-ignore: JSON role and field values are ReplayLab protocol identifiers.
  request[lit("role")] = lit("user");
  request[lit("content")] = prompt;    // i18n-ignore: AgentHost wire-protocol key
  if((supportsV5 || supportsV4 || supportsV3) && !attachments.isEmpty())
    request[lit("attachments")] =    // i18n-ignore: AgentHost attachment protocol key
        chatAttachmentsToJSON(attachments);    // i18n-ignore: AgentHost wire-protocol key

  QJsonArray selected;
  QJsonObject envelope;
  // i18n-ignore: schema and JSON keys are the versioned AgentHost wire protocol.
  envelope[lit("schema")] =
      supportsV5 ? lit("replaylab-conversation/v5")    // i18n-ignore: AgentHost schema IDs
                 : (supportsV4 ? lit("replaylab-conversation/v4")    // i18n-ignore: AgentHost schema IDs
                               : (supportsV3 ? lit("replaylab-conversation/v3")    // i18n-ignore: AgentHost schema IDs
                                             : lit("replaylab-conversation/v2")));    // i18n-ignore: AgentHost schema ID
  envelope[lit("conversationId")] = thread->id;    // i18n-ignore: AgentHost wire-protocol key
  envelope[lit("request")] = request;    // i18n-ignore: AgentHost wire-protocol key
  envelope[lit("messages")] = selected;    // i18n-ignore: AgentHost wire-protocol key
  // i18n-ignore: AgentHost wire-protocol key for bounded-history accounting
  envelope[lit("omittedMessageCount")] =
      qMin(1000000, omittedHistoryCount + history.size());

  if(clarificationResponse.isValid())
  {
    QJsonObject response;
    // i18n-ignore: ReplayLab structured clarification-response wire-protocol fields.
    response[lit("schema")] = lit("replaylab-clarification-response/v1");
    response[lit("clarificationId")] =    // i18n-ignore: clarification-response protocol key
        clarificationResponse.clarificationId;
    QJsonArray optionIds;
    for(const QString &optionId : clarificationResponse.optionIds)
      optionIds.push_back(optionId);
    response[lit("optionIds")] = optionIds;    // i18n-ignore: clarification-response protocol key
    response[lit("otherText")] =    // i18n-ignore: clarification-response protocol key
        clarificationResponse.otherText;
    if(!clarificationResponse.originRequest.isEmpty())
      // i18n-ignore: clarification-response protocol field. The value is original user content.
      response[lit("originRequest")] = clarificationResponse.originRequest;
    if(!clarificationResponse.originRequestSha256.isEmpty())
      // i18n-ignore: clarification-response protocol SHA-256 field.
      response[lit("originRequestSha256")] = clarificationResponse.originRequestSha256;
    envelope[lit("clarificationResponse")] = response;    // i18n-ignore: conversation protocol key

    QJsonObject candidates;
    candidates[lit("schema")] =    // i18n-ignore: context-candidate protocol key
        lit("replaylab-context-candidates/v1");    // i18n-ignore: context-candidate schema ID
    const QJsonObject origin =
        contextCandidateJSON(clarificationResponse.originContextSnapshot,
                             clarificationResponse.originContextInstanceId);
    const QJsonObject current =
        contextCandidateJSON(clarificationResponse.currentContextSnapshot,
                             clarificationResponse.currentContextInstanceId);
    if(!origin.isEmpty())
      candidates[lit("origin")] = origin;    // i18n-ignore: context-candidate protocol key
    if(!current.isEmpty())
      candidates[lit("current")] = current;    // i18n-ignore: context-candidate protocol key
    if(candidates.size() > 1)
      envelope[lit("contextCandidates")] = candidates;    // i18n-ignore: conversation protocol key
  }

  // Validate the current request envelope before considering history. In particular, base64 file
  // content expands on the wire and must never be hidden by the history-trimming loop.
  if(QJsonDocument(envelope).toJson(QJsonDocument::Compact).size() > MaxPromptBytes)
    return QString::fromUtf8(QJsonDocument(envelope).toJson(QJsonDocument::Compact));

  // Keep one contiguous newest-first suffix of complete messages. The 16 MiB
  // transport gate does not imply that AgentHost has summarized earlier turns:
  // disclose any omitted prefix so the Agent can distinguish it from a complete history.
  for(int i = history.size() - 1; i >= 0; --i)
  {
    const AIChatMessage *item = history[i];
    if(!schemaSupportsAttachments(inputSchemas, item->attachments))
    {
      // A v3 host cannot represent an older v4 binary attachment. Keep only the exact newer
      // suffix and report the inaccessible prefix through omittedMessageCount.
      envelope[lit("omittedMessageCount")] =    // i18n-ignore: AgentHost wire-protocol key
          qMin(1000000, omittedHistoryCount + i + 1);
      break;
    }
    QJsonObject message;
    // i18n-ignore: role values and keys are machine-readable protocol identifiers.
    message[lit("id")] = item->id;
    message[lit("role")] = item->role;    // i18n-ignore: AgentHost wire-protocol key
    message[lit("content")] = item->text;    // i18n-ignore: AgentHost wire-protocol key
    if((supportsV5 || supportsV4 || supportsV3) && !item->attachments.isEmpty())
      message[lit("attachments")] =    // i18n-ignore: AgentHost attachment protocol key
          chatAttachmentsToJSON(item->attachments);    // i18n-ignore: AgentHost wire-protocol key
    selected.insert(0, message);
    envelope[lit("messages")] = selected;    // i18n-ignore: AgentHost wire-protocol key
    envelope[lit("omittedMessageCount")] =    // i18n-ignore: AgentHost wire-protocol key
        qMin(1000000, omittedHistoryCount + i);
    if(QJsonDocument(envelope).toJson(QJsonDocument::Compact).size() > MaxPromptBytes)
    {
      selected.removeAt(0);
      envelope[lit("messages")] = selected;    // i18n-ignore: AgentHost wire-protocol key
      envelope[lit("omittedMessageCount")] =    // i18n-ignore: AgentHost wire-protocol key
          qMin(1000000, omittedHistoryCount + i + 1);
      break;
    }
  }
  return QString::fromUtf8(QJsonDocument(envelope).toJson(QJsonDocument::Compact));
}

void AIAssistant::advanceChatContextFreeze()
{
  if(m_ShuttingDown || !m_ChatContextFreezeJobId.isEmpty())
  {
    updateChatContextFreezeTimer();
    return;
  }

  int pendingIndex = -1;
  for(int i = 0; i < m_ChatQueue.size(); ++i)
  {
    const PendingChat &candidate = m_ChatQueue[i];
    if(candidate.contextFreezeRequired && candidate.contextSnapshot.isEmpty() &&
       candidate.contextFreezeJobId.isEmpty())
    {
      pendingIndex = i;
      break;
    }
  }
  if(pendingIndex < 0)
  {
    updateChatContextFreezeTimer();
    return;
  }

  // Copy every identity before entering the embedded integration. Do not retain a QVector
  // reference, ConversationThread pointer, or message pointer across that boundary.
  const PendingChat pending = m_ChatQueue[pendingIndex];
  QString currentInstance;
  QString currentSession;
  const bool captureLoaded = m_Ctx.IsCaptureLoaded();
  const bool bridgeBound = captureLoaded && findBridgeBinding(&currentInstance, &currentSession);
  if(captureLoaded && !bridgeBound)
  {
    // Project restoration can keep the replay bridge busy long enough for its heartbeat to look
    // temporarily stale. The request already holds its submit-time instance and session; wait
    // for that exact binding instead of failing a valid user message at the first missed poll.
    const qint64 now = QDateTime::currentMSecsSinceEpoch();
    if(m_ChatBridgeRetryTaskId != pending.taskId)
    {
      m_ChatBridgeRetryTaskId = pending.taskId;
      m_ChatBridgeRetryDeadlineMs = now + ChatBridgeReadinessTimeoutMs;
    }
    if(now < m_ChatBridgeRetryDeadlineMs)
    {
      if(!m_ChatBridgeRetryScheduled)
      {
        m_ChatBridgeRetryScheduled = true;
        QTimer::singleShot(ChatBridgeReadinessPollIntervalMs, this, [this]() {
          m_ChatBridgeRetryScheduled = false;
          advanceChatContextFreeze();
        });
      }
      return;
    }
  }
  m_ChatBridgeRetryTaskId.clear();
  m_ChatBridgeRetryDeadlineMs = 0;
  if(!captureLoaded || !bridgeBound || currentInstance != pending.contextInstanceId ||
     currentSession != pending.requestedCaptureSessionId)
  {
    failQueuedChatContextFreeze(
        pending.taskId, pending.contextFreezeJobId,
        tr("The capture bridge is not ready yet."));
    QTimer::singleShot(0, this, [this]() { advanceChatContextFreeze(); });
    return;
  }

  QString bridgeError;
  const QByteArray encoded =
      m_Ctx.ReplayLab()
          .BeginCurrentContextFreezeJSON(
              pending.focusStage, pending.requestedEventId, pending.requestedSelectedEventId,
              pending.requestedCaptureSessionId, &bridgeError)
          .toUtf8();
  ChatContextFreezeEnvelope envelope;
  const bool validEnvelope = decodeChatContextFreezeEnvelope(encoded, false, envelope);
  QString pathError;
  const QString resultPath =
      validEnvelope
          ? m_Ctx.ReplayLab().ValidateCurrentContextFreezeResultPath(
                envelope.jobId, envelope.resultPath, &pathError)
          : QString();
  if(!validEnvelope || resultPath.isEmpty() ||
     envelope.captureSessionId != pending.requestedCaptureSessionId ||
     envelope.currentEventId != pending.requestedEventId ||
     envelope.selectedEventId != pending.requestedSelectedEventId)
  {
    const QString diagnostic = !bridgeError.isEmpty() ? bridgeError : pathError;
    if(!diagnostic.isEmpty())
      // i18n-ignore: developer diagnostic written only to the debug log.
      qWarning() << QCoreApplication::translate("AIAssistant", "Could not begin asynchronous Bread AI context freeze:") << diagnostic;
    else
      // i18n-ignore: developer diagnostic written only to the debug log.
      qWarning() << QCoreApplication::translate("AIAssistant", "Bread returned an invalid asynchronous context-freeze envelope");

    // A non-empty but malformed reply could have queued replay work before serialization failed.
    // Keep a local drain barrier instead of risking a second Python Begin while that callback may
    // own the GIL. A capture lifecycle transition may safely discard this unobservable barrier.
    if(!encoded.isEmpty())
    {
      m_ChatContextFreezeTaskId = pending.taskId;
      m_ChatContextFreezeJobId = lit("protocol-error");    // i18n-ignore: internal sentinel
      m_ChatContextFreezeTimedOut = true;
    }
    failQueuedChatContextFreeze(
        pending.taskId, pending.contextFreezeJobId,
        tr("The current RenderDoc state could not be frozen for this AI request."));
    if(encoded.isEmpty())
      QTimer::singleShot(0, this, [this]() { advanceChatContextFreeze(); });
    return;
  }

  // The Begin call is constant-time and does not spin the Qt event loop, but still re-find by
  // stable ID so this invariant remains safe if that implementation changes later.
  int acceptedIndex = -1;
  for(int i = 0; i < m_ChatQueue.size(); ++i)
  {
    const PendingChat &candidate = m_ChatQueue[i];
    if(candidate.taskId == pending.taskId && candidate.contextFreezeRequired &&
       candidate.contextSnapshot.isEmpty() && candidate.contextFreezeJobId.isEmpty() &&
       candidate.requestedCaptureSessionId == pending.requestedCaptureSessionId &&
       candidate.requestedEventId == pending.requestedEventId &&
       candidate.requestedSelectedEventId == pending.requestedSelectedEventId)
    {
      acceptedIndex = i;
      break;
    }
  }
  if(acceptedIndex < 0)
  {
    // The request was locally replaced while Begin was executing. Drain its result file before
    // beginning another job, but never attach the obsolete context to a newer edit.
    m_ChatContextFreezeTaskId.clear();
  }
  else
  {
    m_ChatQueue[acceptedIndex].contextFreezeJobId = envelope.jobId;
    m_ChatContextFreezeTaskId = pending.taskId;
  }
  m_ChatContextFreezeJobId = envelope.jobId;
  m_ChatContextFreezeResultPath = resultPath;
  m_ChatContextFreezeCaptureSessionId = envelope.captureSessionId;
  m_ChatContextFreezeInstanceId = pending.contextInstanceId;
  m_ChatContextFreezeEventId = envelope.currentEventId;
  m_ChatContextFreezeSelectedEventId = envelope.selectedEventId;
  m_ChatContextFreezeDeadlineMs =
      QDateTime::currentMSecsSinceEpoch() + ChatContextFreezeTimeoutMs;
  m_ChatContextFreezeTimedOut = false;
  updateChatContextFreezeTimer();
  updateComposerState();
  QTimer::singleShot(0, this, [this]() { pollChatContextFreeze(); });
}

void AIAssistant::updateChatContextFreezeTimer()
{
  if(!m_ChatContextFreezeTimer)
    return;
  const bool shouldPoll = !m_ShuttingDown && !m_ChatContextFreezeJobId.isEmpty() &&
                          !m_ChatContextFreezeResultPath.isEmpty();
  if(shouldPoll && !m_ChatContextFreezeTimer->isActive())
    m_ChatContextFreezeTimer->start();
  else if(!shouldPoll)
    m_ChatContextFreezeTimer->stop();
}

void AIAssistant::pollChatContextFreeze()
{
  if(m_ShuttingDown || m_ChatContextFreezeJobId.isEmpty() ||
     m_ChatContextFreezeResultPath.isEmpty())
  {
    updateChatContextFreezeTimer();
    return;
  }

  const QString activeTaskId = m_ChatContextFreezeTaskId;
  const QString activeJobId = m_ChatContextFreezeJobId;
  const QString activePath = m_ChatContextFreezeResultPath;
  const QString activeSession = m_ChatContextFreezeCaptureSessionId;
  const qint64 activeEventId = m_ChatContextFreezeEventId;
  const qint64 activeSelectedEventId = m_ChatContextFreezeSelectedEventId;
  const QFileInfo resultInfo(activePath);
  if(resultInfo.exists())
  {
    // Revalidate after the terminal file appears. This second gate additionally proves that the
    // target is a regular file rather than a symlink before any bytes are opened.
    QString pathError;
    const QString validatedPath =
        m_Ctx.ReplayLab().ValidateCurrentContextFreezeResultPath(activeJobId, activePath,
                                                                 &pathError);
    QByteArray encoded;
    if(!validatedPath.isEmpty())
    {
      QFile resultFile(validatedPath);
      if(resultFile.open(QIODevice::ReadOnly) && resultFile.size() > 0 &&
         resultFile.size() <= MaxContextFreezeResultBytes)
        encoded = resultFile.readAll();
    }

    ChatContextFreezeEnvelope envelope;
    const bool validEnvelope = decodeChatContextFreezeEnvelope(encoded, true, envelope);
    // Even if the context payload itself is invalid, an exact terminal header proves the one
    // replay callback released the GIL. It is then safe to retire the drain barrier and fail this
    // request instead of deadlocking every later submission.
    // i18n-ignore: ReplayLab asynchronous context-freeze lifecycle states.
    const bool terminalStatus = envelope.status == lit("completed") ||
                                // i18n-ignore: ReplayLab context-freeze lifecycle state.
                                envelope.status == lit("failed") ||
                                // i18n-ignore: ReplayLab context-freeze lifecycle state.
                                envelope.status == lit("abandoned");
    const bool matchingTerminal = terminalStatus && envelope.jobId == activeJobId &&
                                  envelope.captureSessionId == activeSession &&
                                  envelope.currentEventId == activeEventId &&
                                  envelope.selectedEventId == activeSelectedEventId &&
                                  !validatedPath.isEmpty() &&
                                  QDir::cleanPath(QDir::fromNativeSeparators(envelope.resultPath)) ==
                                      validatedPath;
    if(matchingTerminal)
    {
      if(!QFile::remove(validatedPath))
        // i18n-ignore: developer diagnostic written only to the debug log.
        qWarning() << QCoreApplication::translate("AIAssistant", "Could not remove consumed Bread AI context result:") << validatedPath;

      m_ChatContextFreezeTaskId.clear();
      m_ChatContextFreezeJobId.clear();
      m_ChatContextFreezeResultPath.clear();
      m_ChatContextFreezeCaptureSessionId.clear();
      m_ChatContextFreezeInstanceId.clear();
      m_ChatContextFreezeEventId = 0;
      m_ChatContextFreezeSelectedEventId = 0;
      m_ChatContextFreezeDeadlineMs = 0;
      m_ChatContextFreezeTimedOut = false;
      updateChatContextFreezeTimer();

      int queuedIndex = -1;
      for(int i = 0; i < m_ChatQueue.size(); ++i)
      {
        if(m_ChatQueue[i].taskId == activeTaskId &&
           m_ChatQueue[i].contextFreezeJobId == activeJobId)
        {
          queuedIndex = i;
          break;
        }
      }
      if(queuedIndex >= 0 && validEnvelope &&
         envelope.status == lit("completed"))    // i18n-ignore: context-freeze lifecycle state
      {
        PendingChat updated = m_ChatQueue[queuedIndex];
        updated.contextSnapshot = envelope.contextSnapshot;
        updated.contextFreezeJobId.clear();
        if(updated.clarificationResponse.isValid())
        {
          updated.clarificationResponse.currentContextSnapshot = updated.contextSnapshot;
          updated.clarificationResponse.currentContextInstanceId = updated.contextInstanceId;
        }
        m_ChatQueue[queuedIndex] = updated;
      }
      else if(queuedIndex >= 0)
      {
        if(!envelope.diagnostic.isEmpty())
          // i18n-ignore: developer diagnostic written only to the debug log.
          qWarning() << QCoreApplication::translate("AIAssistant", "Bread AI context freeze failed:") << envelope.diagnostic;
        failQueuedChatContextFreeze(
            activeTaskId, activeJobId,
            tr("The current RenderDoc state could not be frozen for this AI request."));
      }

      refreshConversationList();
      updateComposerState();
      scheduleConversationSave();
      QTimer::singleShot(0, this, [this]() {
        advanceChatContextFreeze();
        startNextChat();
      });
      return;
    }
  }

  if(!m_ChatContextFreezeTimedOut && m_ChatContextFreezeDeadlineMs > 0 &&
     QDateTime::currentMSecsSinceEpoch() >= m_ChatContextFreezeDeadlineMs)
  {
    m_ChatContextFreezeTimedOut = true;
    // Do not call Python Abandon and do not begin another job. The replay callback may still hold
    // the GIL; keep polling its terminal file as a drain barrier while the rest of the UI remains
    // responsive.
    failQueuedChatContextFreeze(
        activeTaskId, activeJobId,
        tr("The current RenderDoc state could not be frozen for this AI request."));
    // i18n-ignore: developer diagnostic written only to the debug log.
    qWarning() << QCoreApplication::translate("AIAssistant", "Timed out waiting for asynchronous Bread AI context freeze") << activeJobId;
  }
}

void AIAssistant::failQueuedChatContextFreeze(const QString &taskId, const QString &jobId,
                                              const QString &diagnostic)
{
  QString conversationId;
  for(int i = 0; i < m_ChatQueue.size(); ++i)
  {
    if(m_ChatQueue[i].taskId == taskId &&
       (jobId.isEmpty() || m_ChatQueue[i].contextFreezeJobId == jobId))
    {
      conversationId = m_ChatQueue[i].conversationId;
      m_ChatQueue.remove(i);
      break;
    }
  }
  if(conversationId.isEmpty())
    return;
  appendSystem(diagnostic, true, conversationId);
  refreshConversationList();
  updateComposerState();
  scheduleConversationSave();
}

void AIAssistant::discardChatContextFreeze(PendingChat &request)
{
  request.contextFreezeJobId.clear();
  request.contextSnapshot.clear();
  request.launchBridgeReadinessDeadlineMs = 0;
}

void AIAssistant::discardAllChatContextFreezes()
{
  m_ChatBridgeRetryTaskId.clear();
  m_ChatBridgeRetryDeadlineMs = 0;
  for(PendingChat &request : m_ChatQueue)
    discardChatContextFreeze(request);

  // This method is used only for panel shutdown or a capture lifecycle transition. At that point
  // the old replay queue cannot authorize work in the next capture, so discard its local token;
  // an already-running Python callback owns no QObject pointer and may only finish its stale file.
  m_ChatContextFreezeTaskId.clear();
  m_ChatContextFreezeJobId.clear();
  m_ChatContextFreezeResultPath.clear();
  m_ChatContextFreezeCaptureSessionId.clear();
  m_ChatContextFreezeInstanceId.clear();
  m_ChatContextFreezeEventId = 0;
  m_ChatContextFreezeSelectedEventId = 0;
  m_ChatContextFreezeDeadlineMs = 0;
  m_ChatContextFreezeTimedOut = false;
  updateChatContextFreezeTimer();
}

void AIAssistant::clearChatContextFile(ChatExecution *execution)
{
  if(!execution || execution->contextFile.isEmpty())
    return;
  const QString path = execution->contextFile;
  execution->contextFile.clear();
  if(!QFile::remove(path))
    // i18n-ignore: developer diagnostic written only to the debug log.
    qWarning() << QCoreApplication::translate("AIAssistant", "Could not remove temporary Bread AI context file:") << path;
}

void AIAssistant::requestChatCancellation(ChatExecution *execution)
{
  if(!execution || execution->cancelFile.isEmpty())
    return;
  QFile signal(execution->cancelFile);
  if(!signal.open(QIODevice::WriteOnly | QIODevice::Truncate) ||
     signal.write("cancel\n") != qint64(sizeof("cancel\n") - 1) || !signal.flush())
  {
    // i18n-ignore: developer diagnostic written only to the debug log.
    qWarning() << QCoreApplication::translate("AIAssistant", "Could not signal Bread AI task cancellation:") << execution->cancelFile;
  }
}

void AIAssistant::clearChatCancelFile(ChatExecution *execution)
{
  if(!execution || execution->cancelFile.isEmpty())
    return;
  const QString path = execution->cancelFile;
  execution->cancelFile.clear();
  if(QFile::exists(path) && !QFile::remove(path))
    // i18n-ignore: developer diagnostic written only to the debug log.
    qWarning() << QCoreApplication::translate("AIAssistant", "Could not remove temporary Bread AI cancellation file:") << path;
}

void AIAssistant::StartCaptureIntent(const QString &prompt,
                                     const QString &focusStage, const QString &intentKind)
{
  // Explicit capture shortcuts start independent work with the clicked EID/
  // Shader context. Preserve the existing conversation and any running task.
  const QString previous = m_ActiveConversationId;
  createConversation();
  if(m_ActiveConversationId == previous)
    return;
  startChat(prompt, false, focusStage, ClarificationResponse(), intentKind);
}

void AIAssistant::startChat(const QString &prompt, bool includeConversation,
                            const QString &focusStage,
                            const ClarificationResponse &clarificationResponse,
                            const QString &intentKind,
                            const QVector<AIChatAttachment> &attachments)
{
  if(m_CLIPath.isEmpty())
  {
    appendSystem(tr("AI host was not found."), true);
    return;
  }
  const QString conversationId = m_ActiveConversationId;
  const ConversationThread *submittedThread = conversation(conversationId);
  if(!submittedThread)
    return;
  const QString submittedProfile = submittedThread->profileId;
  const QString submittedModel = submittedThread->modelId;
  if(submittedProfile.isEmpty())
  {
    appendSystem(tr("Add or select an AI connection first."), true);
    return;
  }

  if(!intentKind.isEmpty() && !m_Ctx.IsCaptureLoaded())
  {
    appendSystem(tr("This action requires an open capture."), true);
    return;
  }

  if(prompt.toUtf8().size() > MaxPromptBytes)
  {
    appendSystem(tr("The request exceeds the 16 MiB safety limit."), true);
    return;
  }
  // The persisted recovery queue has the same global bound. Keep the in-memory
  // queue within that envelope so a crash/restart never silently drops turns
  // accepted from other conversations.
  if(m_ChatQueue.size() >= MaxPendingChats)
  {
    appendSystem(tr("The pending-message queue is full. Remove a queued message before sending "
                    "another."),
                 true);
    return;
  }
  const QStringList inputSchemas = m_ProfileRunInputSchemas.value(submittedProfile);
  if(!schemaSupportsAttachments(inputSchemas, attachments))
  {
    appendSystem(attachmentsNeedV5(attachments)
                     ? tr("The selected AI host does not support linked context or local file "
                          "references. Update the bundled AI host or remove those "
                          "attachments.")
                     : (attachmentsNeedV4(attachments)
                            ? tr("The selected AI host does not support image or file attachments. "
                                 "Update the bundled AI host or remove those attachments.")
                            : tr("The selected AI host does not support structured attachments. "
                                 "Update the bundled AI host or remove the attachments.")),
                 true);
    return;
  }

  ClarificationResponse effectiveClarification = clarificationResponse;
  if(!effectiveClarification.isValid() && submittedThread->clarification.isValid())
  {
    // A typed reply is still a response to the visible question even when the user does not click
    // Other first. Preserve it verbatim as otherText and let AgentHost determine whether it
    // answers the question, changes the goal, or requires an origin/current context choice.
    effectiveClarification.clarificationId = submittedThread->clarification.id;
    effectiveClarification.otherText = prompt.left(MaxClarificationOtherChars);
    effectiveClarification.originRequest = submittedThread->clarification.originRequest;
    effectiveClarification.originRequestSha256 =
        submittedThread->clarification.originRequestSha256;
    effectiveClarification.originContextSnapshot =
        submittedThread->clarification.originContextSnapshot;
    effectiveClarification.originContextInstanceId =
        submittedThread->clarification.originContextInstanceId;
  }
  submittedThread = NULL;

  PendingChat request;
  request.taskId = newTaskId();
  request.conversationId = conversationId;
  request.profileId = submittedProfile;
  request.modelId = submittedModel;
  request.prompt = prompt;
  request.effectivePrompt = prompt;
  // Only this message's explicit @skill mention pre-loads guidance. Otherwise
  // the Agent chooses whether to load a Skill during the turn.
  const QStringList mentioned = mentionedSkillIds(prompt);
  if(!mentioned.isEmpty())
    request.skill = mentioned.first();
  request.mcpServerIds = mentionedMcpServerIds(prompt);
  request.focusStage = focusStage;
  request.intentKind = intentKind;
  request.attachments = attachments;
  request.clarificationResponse = effectiveClarification;
  request.includeConversation = includeConversation;
  if(m_Ctx.IsCaptureLoaded())
  {
    if(findBridgeBinding(&request.contextInstanceId, &request.requestedCaptureSessionId))
    {
      request.contextFreezeRequired = true;
      request.requestedEventId = qint64(m_Ctx.CurEvent());
      request.requestedSelectedEventId = qint64(m_Ctx.CurSelectedEvent());
    }
    // A missing bridge means no capture tools for this turn. It does not make
    // an ordinary conversation or a general Skill request unsendable.
  }
  clearClarification(request.conversationId);
  ConversationThread *thread = conversation(request.conversationId);
  if(!thread)
    return;
  thread->updatedMs = QDateTime::currentMSecsSinceEpoch();
  thread->unread = false;
  if(thread->title == tr("New conversation") || thread->title.trimmed().isEmpty())
  {
    QString title = prompt.simplified();
    if(title.size() > 52)
      title = title.left(49) + lit("...");
    if(!title.isEmpty())
      thread->title = title;
  }
  m_ChatQueue.push_back(request);
  if(m_Transcript && request.conversationId == m_ActiveConversationId)
    m_Transcript->followSubmittedTurn();
  refreshConversationList();
  updateComposerState();
  // Snapshot the accepted user request exactly once before asynchronous freeze/launch can remove
  // it from the recovery queue. The serialized writer owns that immutable generation even if this
  // panel or capture closes, without delaying launch for disk I/O. Later transcript and streaming
  // mutations use the trailing debounce.
  saveConversations();
  // At most one Begin can enter embedded Python at a time. Later submissions remain entirely
  // native and retain their submit-time capture session/current EID/selected EID until the active
  // replay callback publishes a terminal file and releases the GIL.
  advanceChatContextFreeze();
  QTimer::singleShot(0, this, [this]() { startNextChat(); });
}

void AIAssistant::startNextChat()
{
  if(m_ShuttingDown)
    return;

  int availableSlots = qMax(0, maxConcurrentAIChats() - m_ChatExecutions.size());

  // Each conversation owns one sequential turn lane, while distinct conversations may run their
  // own AgentHost processes concurrently. Scan past a busy or still-freezing conversation so it
  // never head-of-line blocks another independent chat.
  QSet<QString> busyConversations;
  for(auto it = m_ChatExecutions.cbegin(); it != m_ChatExecutions.cend(); ++it)
    if(it.value())
      busyConversations.insert(it.value()->request.conversationId);

  for(int index = 0; index < m_ChatQueue.size() && availableSlots > 0;)
  {
    const PendingChat request = m_ChatQueue[index];
    if(busyConversations.contains(request.conversationId))
    {
      ++index;
      continue;
    }
    if(request.contextFreezeRequired && request.contextSnapshot.isEmpty())
    {
      // This earlier turn is a FIFO barrier for its conversation. Continue
      // scanning so another conversation can launch, but never let a later
      // already-frozen turn from this conversation overtake it.
      busyConversations.insert(request.conversationId);
      ++index;
      continue;
    }
    // A frozen request can wait behind another turn long enough for the bridge
    // heartbeat to go stale. Keep its original session/EIDs and queue position
    // while discovery recovers, just as the context-freeze path already does.
    // Other conversations remain runnable. A changed binding or expired wait
    // still reaches launchChat's exact identity check and fails closed.
    if(!request.contextSnapshot.isEmpty() && m_Ctx.IsCaptureLoaded())
    {
      QString instance, session;
      if(!findBridgeBinding(&instance, &session))
      {
        const qint64 now = QDateTime::currentMSecsSinceEpoch();
        qint64 &deadline = m_ChatQueue[index].launchBridgeReadinessDeadlineMs;
        if(deadline == 0)
          deadline = now + ChatBridgeReadinessTimeoutMs;
        if(now < deadline)
        {
          busyConversations.insert(request.conversationId);
          if(!m_ChatLaunchRetryScheduled)
          {
            m_ChatLaunchRetryScheduled = true;
            QTimer::singleShot(ChatBridgeReadinessPollIntervalMs, this, [this]() {
              m_ChatLaunchRetryScheduled = false;
              startNextChat();
            });
          }
          ++index;
          continue;
        }
      }
    }
    m_ChatQueue.remove(index);
    // The accepted queue snapshot was captured at submit time. Coalesce its transition into a
    // running transcript (or removal of an invalid stale entry) behind the GUI.
    scheduleConversationSave();
    if(conversation(request.conversationId) &&
       (request.messageId.isEmpty() || message(request.conversationId, request.messageId)))
    {
      busyConversations.insert(request.conversationId);
      launchChat(request);
      availableSlots--;
      continue;
    }
    m_UserPrompts.remove(request.taskId);
    unwatchTask(request.taskId);
    m_RunningTasks.remove(request.taskId);
    m_TaskOffsets.remove(request.taskId);
    m_TaskSequences.remove(request.taskId);
    m_TaskTails.remove(request.taskId);
    m_TaskTexts.remove(request.taskId);
    m_TaskAgentMessageStreams.remove(request.taskId);
    m_TaskConversationIds.remove(request.taskId);
    m_TaskMessageIds.remove(request.taskId);
  }
  advanceChatContextFreeze();
  updateComposerState();
}

void AIAssistant::launchChat(PendingChat request)
{
  if(request.messageId.isEmpty())
    request.messageId = appendUser(request.prompt, request.taskId, lit("queued"),    // i18n-ignore: internal message-state ID
                                   request.conversationId, request.attachments);
  if(!request.messageId.isEmpty())
  {
    m_TaskConversationIds[request.taskId] = request.conversationId;
    m_TaskMessageIds[request.taskId] = request.messageId;
  }
  AIChatMessage *chatMessage = message(request.conversationId, request.messageId);
  if(!chatMessage)
  {
    m_UserPrompts.remove(request.taskId);
    unwatchTask(request.taskId);
    m_RunningTasks.remove(request.taskId);
    m_TaskOffsets.remove(request.taskId);
    m_TaskSequences.remove(request.taskId);
    m_TaskTails.remove(request.taskId);
    m_TaskTexts.remove(request.taskId);
    m_TaskAgentMessageStreams.remove(request.taskId);
    m_TaskConversationIds.remove(request.taskId);
    m_TaskMessageIds.remove(request.taskId);
    QTimer::singleShot(0, this, [this]() { startNextChat(); });
    return;
  }
  // A queued request may outlive a connection/profile selection change. Keep its typed reference
  // data on the visible user turn, then fail explicitly if the newly selected host cannot carry
  // a compatible envelope; never downgrade by silently omitting attachments.
  chatMessage->attachments = request.attachments;

  QString contextFilePath;
  QString cancelFilePath;
  auto failBeforeStart = [this, chatMessage, request, &contextFilePath,
                          &cancelFilePath](const QString &failure) {
    if(!contextFilePath.isEmpty() && QFile::exists(contextFilePath) &&
       !QFile::remove(contextFilePath))
      qWarning() << QCoreApplication::translate("AIAssistant", "Could not remove temporary Bread AI context file:") << contextFilePath;
    if(!cancelFilePath.isEmpty() && QFile::exists(cancelFilePath) &&
       !QFile::remove(cancelFilePath))
      qWarning() << QCoreApplication::translate("AIAssistant", "Could not remove temporary Bread AI cancellation file:")
                 << cancelFilePath;
    chatMessage->state = lit("failed");
    chatMessage->provisionalText = failure;
    chatMessage->finishedTimestampMs = QDateTime::currentMSecsSinceEpoch();
    AIActivityItem item;
    item.kind = lit("failed");
    item.title = tr("Could not start");
    item.detail = failure;
    item.timestampMs = chatMessage->finishedTimestampMs;
    item.finishedTimestampMs = chatMessage->finishedTimestampMs;
    item.completed = true;
    item.error = true;
    chatMessage->activity.push_back(item);
    chatMessage->activityExpanded = false;
    m_UserPrompts.remove(request.taskId);
    unwatchTask(request.taskId);
    m_RunningTasks.remove(request.taskId);
    m_TaskOffsets.remove(request.taskId);
    m_TaskSequences.remove(request.taskId);
    m_TaskTails.remove(request.taskId);
    m_TaskTexts.remove(request.taskId);
    m_TaskAgentMessageStreams.remove(request.taskId);
    m_TaskConversationIds.remove(request.taskId);
    m_TaskMessageIds.remove(request.taskId);
    renderConversation();
    saveConversations();
    QTimer::singleShot(0, this, [this]() { startNextChat(); });
  };

  if(request.profileId.isEmpty() || !m_ProfileRunInputSchemas.contains(request.profileId))
  {
    failBeforeStart(tr("The selected AI connection is no longer available."));
    return;
  }
  const QStringList inputSchemas = m_ProfileRunInputSchemas.value(request.profileId);
  if(!schemaSupportsAttachments(inputSchemas, request.attachments))
  {
    failBeforeStart(
        attachmentsNeedV5(request.attachments)
            ? tr("The selected AI host does not support linked context or local file references. "
                 "Update the bundled AI host or remove those attachments.")
            : (attachmentsNeedV4(request.attachments)
                   ? tr("The selected AI host does not support image or file attachments. Update "
                        "the bundled AI host or remove those attachments.")
                   : tr("The selected AI host does not support structured attachments. Update "
                        "the bundled AI host or remove the attachments.")));
    return;
  }

  QString instance;
  if(!request.contextSnapshot.isEmpty())
  {
    if(!m_Ctx.IsCaptureLoaded())
    {
      failBeforeStart(tr("This action requires an open capture."));
      return;
    }
    QString captureSessionId;
    if(!findBridgeBinding(&instance, &captureSessionId) ||
       instance != request.contextInstanceId ||
       captureSessionId != request.requestedCaptureSessionId)
    {
      failBeforeStart(tr("The capture bridge is not ready yet."));
      return;
    }
    m_InstanceId = instance;
  }
  else if(request.contextFreezeRequired)
  {
    failBeforeStart(tr("The current RenderDoc state could not be frozen for this AI request."));
    return;
  }

  QStringList excludedMessages = request.historyExcludeMessageIds;
  excludedMessages.push_back(request.messageId);
  // The envelope is also the only carrier for an answer to a pending question.
  // Without it the question was cleared from the composer and its response
  // silently dropped before the Agent could read it.
  const bool carriesClarification = request.clarificationResponse.isValid();
  const QString submittedPrompt =
      (request.includeConversation || !request.attachments.isEmpty() || carriesClarification)
          ? conversationPrompt(request.effectivePrompt, request.conversationId, excludedMessages,
                               request.clarificationResponse, request.attachments,
                               request.profileId)
          : request.effectivePrompt;
  const QByteArray promptBytes = submittedPrompt.toUtf8();
  if(promptBytes.size() > MaxPromptBytes)
  {
    failBeforeStart(tr("The request plus conversation context exceeds the 16 MiB safety limit."));
    return;
  }
  // i18n-ignore: Internal AgentHost CLI command/options; never shown as UI text.
  QStringList arguments = {lit("run"), lit("--profile"), request.profileId,
                           lit("--prompt-stdin"),    // i18n-ignore: Internal AgentHost CLI option.
                           lit("--task-id"), request.taskId,
                           lit("--conversation-id"),    // i18n-ignore: Internal AgentHost CLI option.
                           request.conversationId, lit("--json")};
  if(!request.modelId.isEmpty())
    // i18n-ignore: Internal AgentHost CLI option; never shown as UI text.
    arguments << lit("--model") << request.modelId;
  if(!request.skill.isEmpty())
    arguments << lit("--skill") << request.skill;
  for(const QString &server : request.mcpServerIds)
    // i18n-ignore: Internal AgentHost CLI option; never shown as UI text.
    arguments << lit("--enable-mcp") << server;
  if(!request.intentKind.isEmpty())
    // i18n-ignore: Stable internal CLI option; never shown as UI text.
    arguments << lit("--intent-kind") << request.intentKind;
  if(!request.contextSnapshot.isEmpty())
  {
    const QString temporaryRoot =
        QStandardPaths::writableLocation(QStandardPaths::TempLocation);
    QTemporaryFile contextFile(
        QDir(temporaryRoot).filePath(lit("ReplayLabAIContext-XXXXXX.json")));    // i18n-ignore: fixed temporary filename
    if(!contextFile.open() ||
       contextFile.write(request.contextSnapshot) != request.contextSnapshot.size() ||
       !contextFile.flush())
    {
      failBeforeStart(tr("The current RenderDoc state could not be frozen for this AI request."));
      return;
    }
    contextFilePath = contextFile.fileName();
    contextFile.close();
    contextFile.setAutoRemove(false);
    arguments << lit("--context-file") << contextFilePath << lit("--instance-id") << instance;    // i18n-ignore: CLI flags
  }

  const QString temporaryRoot =
      QStandardPaths::writableLocation(QStandardPaths::TempLocation);
  QTemporaryFile cancelFile(
      QDir(temporaryRoot).filePath(lit("ReplayLabAICancel-XXXXXX.signal")));    // i18n-ignore: fixed temporary filename
  if(!cancelFile.open() || !cancelFile.flush())
  {
    failBeforeStart(tr("The AI cancellation channel could not be created."));
    return;
  }
  cancelFilePath = cancelFile.fileName();
  cancelFile.setAutoRemove(false);
  cancelFile.close();
  arguments << lit("--cancel-file") << cancelFilePath;    // i18n-ignore: CLI flags

  // Retain the effective request (including earlier direction adjustments) so another steer can
  // restart the same work without dropping its original objective.
  watchTask(request.taskId, request.effectivePrompt, request.conversationId, request.messageId);
  ChatExecution *execution = new ChatExecution();
  execution->request = request;
  execution->contextFile = contextFilePath;
  execution->cancelFile = cancelFilePath;
  execution->pendingInput = promptBytes;
  execution->process = new QProcess(this);
  execution->startTimer = new QTimer(this);
  execution->startTimer->setSingleShot(true);
  execution->startTimer->setInterval(3000);
  m_ChatExecutions.insert(request.taskId, execution);
  chatMessage->state = lit("running");
  chatMessage->activityExpanded = true;
  addTaskActivity(request.taskId, lit("running"), tr("Starting Agent"));
  refreshConversationList();
  QProcess *chatProcess = execution->process;
  configureProcess(chatProcess, request.profileId, &execution->processInformation);
  connect(execution->startTimer, &QTimer::timeout, this, [this, chatProcess]() {
    if(chatExecution(chatProcess))
      failChatProcessStart(chatProcess, tr("The AI host did not start in time."));
  });
  connect(chatProcess, &QProcess::readyReadStandardOutput, this, [this, chatProcess]() {
    const QByteArray outputChunk = chatProcess->readAllStandardOutput();
    ChatExecution *current = chatExecution(chatProcess);
    if(current && current->stdoutData.size() < MaxProcessOutputBytes)
      current->stdoutData +=
          outputChunk.left(MaxProcessOutputBytes - current->stdoutData.size());
  });
  connect(chatProcess, &QProcess::readyReadStandardError, this, [this, chatProcess]() {
    const QByteArray errorChunk = chatProcess->readAllStandardError();
    ChatExecution *current = chatExecution(chatProcess);
    if(current && current->stderrData.size() < MaxEventLineBytes)
      current->stderrData += errorChunk.left(MaxEventLineBytes - current->stderrData.size());
  });
  connect(chatProcess, &QProcess::started, this,
          [this, chatProcess]() { handleChatProcessStarted(chatProcess); });
  connect(chatProcess, &QProcess::errorOccurred, this,
          [this, chatProcess](QProcess::ProcessError error) {
            ChatExecution *current = chatExecution(chatProcess);
            if(!current)
              return;

            const QString diagnostic = chatProcess->errorString().trimmed();
            if(!diagnostic.isEmpty() && current->stderrData.isEmpty())
              current->stderrData = diagnostic.toUtf8().left(MaxEventLineBytes);

            // FailedToStart has no matching finished signal. Any error before the complete prompt
            // was handed off is likewise a launch failure, so retire it without blocking the UI.
            if(error == QProcess::FailedToStart || !current->promptSent)
              failChatProcessStart(chatProcess,
                                   diagnostic.isEmpty() ? tr("Could not start") : diagnostic);
          });
  connect(chatProcess,
          static_cast<void (QProcess::*)(int, QProcess::ExitStatus)>(&QProcess::finished), this,
          [this, chatProcess](int exitCode, QProcess::ExitStatus exitStatus) {
            ChatExecution *current = chatExecution(chatProcess);
            if(!current)
              return;
            const QString taskId = current->request.taskId;
            if(exitStatus != QProcess::NormalExit && current->stderrData.isEmpty())
              current->stderrData = tr("AI request process terminated unexpectedly.").toUtf8();
            finishChatProcess(taskId, exitStatus == QProcess::NormalExit ? exitCode : -1);
          });
  execution->startTimer->start();
  chatProcess->start(m_CLIPath, arguments, QIODevice::ReadWrite);
  updateComposerState();
}

void AIAssistant::handleChatProcessStarted(QProcess *process)
{
  ChatExecution *execution = chatExecution(process);
  if(!execution || execution->promptSent || m_ShuttingDown)
    return;
  if(execution->startTimer)
    execution->startTimer->stop();

  // `run --prompt-stdin` cannot launch a provider until it receives this prompt. Contain the
  // AgentHost process first so every subsequently-created Codex/MCP child inherits the Job.
  QString jobError;
  if(!attachKillOnCloseJob(process, execution->jobHandle, &jobError))
  {
    failChatProcessStart(process, jobError);
    return;
  }

  qint64 promptOffset = 0;
  while(promptOffset < execution->pendingInput.size())
  {
    const qint64 written = process->write(execution->pendingInput.constData() + promptOffset,
                                          execution->pendingInput.size() - promptOffset);
    if(chatExecution(process) != execution)
      return;
    if(written <= 0)
    {
      failChatProcessStart(process, tr("Failed to send the request to the AI host."));
      return;
    }
    promptOffset += written;
  }

  execution->pendingInput.clear();
  execution->promptSent = true;
  process->closeWriteChannel();
  if(!resumeSuspendedProcess(execution->processInformation, &jobError))
  {
    failChatProcessStart(process, jobError);
    return;
  }
  updateComposerState();
  if(execution->request.conversationId == m_ActiveConversationId)
    setDiagnosticStatus(m_Status, DiagnosticLevel::Info, tr("Thinking..."));
}

void AIAssistant::failChatProcessStart(QProcess *process, const QString &diagnostic)
{
  ChatExecution *execution = chatExecution(process);
  if(!execution)
    return;
  const QString taskId = execution->request.taskId;
  if(execution->startTimer)
    execution->startTimer->stop();
  execution->pendingInput.clear();
  execution->promptSent = false;
  if(!diagnostic.trimmed().isEmpty())
    execution->stderrData = diagnostic.trimmed().toUtf8().left(MaxEventLineBytes);

  // Detach every callback before dropping the current-process identity. A late started/error/
  // finished notification can then neither finish this task twice nor affect the next queued task.
  disconnect(process, NULL, this, NULL);
  execution->process = NULL;
  closeKillOnCloseJob(execution->jobHandle);
  if(process->state() != QProcess::NotRunning)
    process->kill();
  if(process->state() == QProcess::NotRunning)
  {
    process->deleteLater();
  }
  else
  {
    // Starting processes do not guarantee a finished signal when CreateProcess fails. Keep the
    // object outside this panel and retire it on either terminal state transition or finished.
    process->setParent(QCoreApplication::instance());
    connect(process, &QProcess::stateChanged, process,
            [process](QProcess::ProcessState state) {
              if(state == QProcess::NotRunning)
                process->deleteLater();
            });
    deleteProcessWhenFinished(process);
    if(process->state() == QProcess::NotRunning)
      process->deleteLater();
  }

  finishChatProcess(taskId, -1);
}

void AIAssistant::adjustQueuedChat(const QString &taskId)
{
  int queuedIndex = -1;
  for(int i = 0; i < m_ChatQueue.size(); ++i)
  {
    if(m_ChatQueue[i].taskId == taskId)
    {
      queuedIndex = i;
      break;
    }
  }
  if(queuedIndex < 0)
    return;

  const PendingChat directionRequest = m_ChatQueue[queuedIndex];
  if(directionRequest.contextFreezeRequired && directionRequest.contextSnapshot.isEmpty())
    return;
  ChatExecution *execution = conversationExecution(directionRequest.conversationId);
  if(!execution)
  {
    appendSystem(tr("Adjust direction is available when this conversation has an active AI "
                    "request. The message remains queued."),
                 true);
    return;
  }

  const QString direction = directionRequest.prompt.trimmed();
  const QString activeTaskId = execution->request.taskId;
  const QString activeMessageId = execution->request.messageId;
  PendingChat request = execution->request;
  request.taskId = directionRequest.taskId;
  request.messageId.clear();
  request.conversationId = directionRequest.conversationId;
  request.profileId = directionRequest.profileId;
  request.modelId = directionRequest.modelId;
  request.prompt = direction;
  // The direction message is a new user action. Bind the restarted turn to
  // the state frozen when that queued direction was sent, not to the older
  // request which is being superseded.
  request.contextSnapshot = directionRequest.contextSnapshot;
  request.contextInstanceId = directionRequest.contextInstanceId;
  request.contextFreezeRequired = directionRequest.contextFreezeRequired;
  request.contextFreezeJobId.clear();
  request.requestedCaptureSessionId = directionRequest.requestedCaptureSessionId;
  request.requestedEventId = directionRequest.requestedEventId;
  request.requestedSelectedEventId = directionRequest.requestedSelectedEventId;
  request.clarificationResponse = directionRequest.clarificationResponse;
  if(!request.historyExcludeMessageIds.contains(activeMessageId))
    request.historyExcludeMessageIds.push_back(activeMessageId);
  QString partialWork = m_TaskTexts.value(activeTaskId).trimmed();
  if(partialWork.isEmpty())
  {
    const AIChatMessage *activeMessage =
        message(request.conversationId, activeMessageId);
    if(activeMessage)
      partialWork = activeMessage->provisionalText.trimmed();
  }
  request.effectivePrompt = execution->request.effectivePrompt;
  if(!partialWork.isEmpty())
  {
    request.effectivePrompt +=
        tr("\n\nDraft work completed before the interruption (preserve useful findings, "
           "but re-check them against the new direction):\n") +
        partialWork.left(256 * 1024);
  }
  request.effectivePrompt +=
      tr("\n\nAdditional direction from the user (re-plan and continue from the preserved "
         "context):\n") +
      direction;
  qint64 attachmentTextBytes = 0;
  qint64 attachmentBinaryBytes = 0;
  for(const AIChatAttachment &attachment : request.attachments)
  {
    qint64 bytes = 0;
    if(!attachmentDecodedBytes(attachment, &bytes))
    {
      appendSystem(tr("The adjusted request contains invalid attachment data. The current request "
                      "was not interrupted."),
                   true);
      return;
    }
    if(isTextAttachment(attachment))
      attachmentTextBytes += bytes;
    else
      attachmentBinaryBytes += bytes;
  }
  for(const AIChatAttachment &attachment : directionRequest.attachments)
  {
    bool duplicate = false;
    for(const AIChatAttachment &existing : request.attachments)
      duplicate |= existing.id == attachment.id ||
                   (existing.kind == attachment.kind && existing.source == attachment.source &&
                    existing.content == attachment.content);
    if(duplicate)
      continue;
    qint64 nextBytes = 0;
    if(!attachmentDecodedBytes(attachment, &nextBytes))
    {
      appendSystem(tr("The adjusted request contains invalid attachment data. The current request "
                      "was not interrupted."),
                   true);
      return;
    }
    const bool textAttachment = isTextAttachment(attachment);
    if(request.attachments.size() >= MaxChatAttachments ||
       (textAttachment && attachmentTextBytes + nextBytes > MaxAttachmentTotalUTF8Bytes) ||
       (!textAttachment &&
        attachmentBinaryBytes + nextBytes > MaxBinaryAttachmentTotalBytes))
    {
      appendSystem(tr("The adjusted request combines too many attachments. The current request "
                      "was not interrupted."),
                   true);
      return;
    }
    request.attachments.push_back(attachment);
    if(textAttachment)
      attachmentTextBytes += nextBytes;
    else
      attachmentBinaryBytes += nextBytes;
  }
  // Same rule as the ordinary send path: the envelope is the only carrier for an
  // answer to a pending question, and steering a running skill action runs with
  // includeConversation=false, so without this the answer is dropped.
  const QString submittedPrompt =
      (request.includeConversation || !request.attachments.isEmpty() ||
       request.clarificationResponse.isValid())
          ? conversationPrompt(request.effectivePrompt, request.conversationId,
                               request.historyExcludeMessageIds,
                               request.clarificationResponse, request.attachments,
                               request.profileId)
          : request.effectivePrompt;
  if(direction.toUtf8().size() > MaxPromptBytes ||
     submittedPrompt.toUtf8().size() > MaxPromptBytes)
  {
    appendSystem(tr("The adjusted request plus conversation context exceeds the 16 MiB safety "
                    "limit. The current request was not interrupted."),
                 true);
    return;
  }

  m_ChatQueue.remove(queuedIndex);
  m_ChatQueue.prepend(request);
  // i18n-ignore: Stable internal task-state protocol value; never shown as UI text.
  addTaskActivity(activeTaskId, lit("steered"), tr("Direction adjusted"),
                  tr("Preserving completed work, interrupting the current turn, and re-planning "
                     "with the queued clarification."),
                  true);
  updateComposerState();
  // i18n-ignore: Stable internal task-state protocol value; never shown as UI text.
  cancelChat(lit("superseded"), true, activeTaskId);
}

void AIAssistant::editQueuedChat(const QString &taskId)
{
  QString edited;
  if(m_PendingChats)
  {
    for(const AIPendingChatItem &item : m_PendingChats->items())
    {
      if(item.id == taskId)
      {
        edited = item.text.trimmed();
        break;
      }
    }
  }
  if(edited.isEmpty())
    return;
  if(edited.toUtf8().size() > MaxPromptBytes)
  {
    appendSystem(tr("The edited request exceeds the 16 MiB safety limit."), true);
    // The inline editor owns only a draft copy. Restore the authoritative queued text when the
    // validation gate rejects that draft.
    updateComposerState();
    return;
  }

  int queuedIndex = -1;
  for(int i = 0; i < m_ChatQueue.size(); ++i)
  {
    if(m_ChatQueue[i].taskId == taskId)
    {
      queuedIndex = i;
      break;
    }
  }
  if(queuedIndex < 0)
    return;

  const PendingChat original = m_ChatQueue[queuedIndex];
  if(original.clarificationResponse.isValid() && edited.size() > MaxClarificationOtherChars)
  {
    appendSystem(tr("The edited clarification response exceeds the 16,384-character safety limit."),
                 true);
    updateComposerState();
    return;
  }

  PendingChat updated = original;
  updated.prompt = edited;
  updated.effectivePrompt = edited;
  updated.historyExcludeMessageIds.clear();
  discardChatContextFreeze(updated);
  if(updated.clarificationResponse.isValid())
  {
    // Once the readable reply is edited it is no longer safe to claim that it represents the
    // originally clicked option IDs. Preserve the clarification identity and origin candidate,
    // but serialize the edited text as the sole free-form answer. The new current candidate is
    // filled only after this edit's asynchronous freeze reaches a validated terminal result.
    updated.clarificationResponse.optionIds.clear();
    updated.clarificationResponse.otherText = edited;
    updated.clarificationResponse.currentContextSnapshot.clear();
    updated.clarificationResponse.currentContextInstanceId.clear();
  }
  updated.contextFreezeRequired = m_Ctx.IsCaptureLoaded();
  updated.requestedEventId = 0;
  updated.requestedSelectedEventId = 0;
  updated.requestedCaptureSessionId.clear();
  updated.contextInstanceId.clear();
  if(updated.contextFreezeRequired)
  {
    if(!findBridgeBinding(&updated.contextInstanceId, &updated.requestedCaptureSessionId))
    {
      appendSystem(tr("The capture bridge is not ready yet."), true,
                   original.conversationId);
      updateComposerState();
      return;
    }
    updated.requestedEventId = qint64(m_Ctx.CurEvent());
    updated.requestedSelectedEventId = qint64(m_Ctx.CurSelectedEvent());
  }

  // Re-find by stable ID after bridge discovery and preserve a newer edit/delete if one somehow
  // arrived through a nested host callback in a downstream integration.
  queuedIndex = -1;
  for(int i = 0; i < m_ChatQueue.size(); ++i)
  {
    if(m_ChatQueue[i].taskId == taskId && m_ChatQueue[i].prompt == original.prompt &&
       m_ChatQueue[i].contextFreezeJobId == original.contextFreezeJobId)
    {
      queuedIndex = i;
      break;
    }
  }
  if(queuedIndex < 0)
    return;
  m_ChatQueue[queuedIndex] = updated;
  if(ConversationThread *thread = conversation(updated.conversationId))
  {
    thread->updatedMs = QDateTime::currentMSecsSinceEpoch();
    if(thread->messages.isEmpty())
    {
      QString title = edited.simplified();
      if(title.size() > 52)
        title = title.left(49) + lit("...");
      thread->title = title.isEmpty() ? tr("New conversation") : title;
    }
  }
  refreshConversationList();
  updateComposerState();
  scheduleConversationSave();
  advanceChatContextFreeze();
}

void AIAssistant::deleteQueuedChat(const QString &taskId)
{
  QString conversationId;
  for(int i = 0; i < m_ChatQueue.size(); ++i)
  {
    if(m_ChatQueue[i].taskId == taskId)
    {
      conversationId = m_ChatQueue[i].conversationId;
      m_ChatQueue.remove(i);
      break;
    }
  }
  if(conversationId.isEmpty())
    return;

  m_UserPrompts.remove(taskId);
  unwatchTask(taskId);
  m_RunningTasks.remove(taskId);
  m_TaskOffsets.remove(taskId);
  m_TaskSequences.remove(taskId);
  m_TaskTails.remove(taskId);
  m_TaskTexts.remove(taskId);
  m_TaskAgentMessageStreams.remove(taskId);
  m_TaskConversationIds.remove(taskId);
  m_TaskMessageIds.remove(taskId);
  if(ConversationThread *thread = conversation(conversationId))
  {
    thread->updatedMs = QDateTime::currentMSecsSinceEpoch();
    bool hasOtherPending = false;
    for(const PendingChat &pending : m_ChatQueue)
      hasOtherPending |= pending.conversationId == conversationId;
    if(thread->messages.isEmpty() && !hasOtherPending)
      thread->title = tr("New conversation");
  }
  refreshConversationList();
  updateComposerState();
  scheduleConversationSave();
  advanceChatContextFreeze();
  QTimer::singleShot(0, this, [this]() { startNextChat(); });
}

void AIAssistant::cancelChat(const QString &terminalState, bool continueQueue,
                             const QString &taskId, int cooperativeWaitMs, bool refreshUI)
{
  QString targetTaskId = taskId;
  if(targetTaskId.isEmpty())
  {
    ChatExecution *active = conversationExecution(m_ActiveConversationId);
    if(active)
      targetTaskId = active->request.taskId;
  }
  ChatExecution *execution = chatExecution(targetTaskId);
  if(!execution)
    return;
  if(execution->startTimer)
    execution->startTimer->stop();
  execution->pendingInput.clear();
  execution->promptSent = false;
  QProcess *chatProcess = execution->process;
  execution->process = NULL;
  if(chatProcess)
  {
    disconnect(chatProcess, NULL, this, NULL);
    // First request cooperative asyncio cancellation. AgentHost then writes the
    // durable terminal event and releases provider/tool children itself.
    requestChatCancellation(execution);
    if(chatProcess->state() != QProcess::NotRunning && cooperativeWaitMs > 0)
      chatProcess->waitForFinished(cooperativeWaitMs);
    // Job close remains the bounded tree-wide fallback. QProcess::kill alone
    // targets only AgentHost.
    closeKillOnCloseJob(execution->jobHandle);
    if(chatProcess->state() != QProcess::NotRunning)
      chatProcess->kill();
    if(chatProcess->state() == QProcess::NotRunning)
      chatProcess->deleteLater();
    else
    {
      chatProcess->setParent(QCoreApplication::instance());
      connect(chatProcess, &QProcess::stateChanged, chatProcess,
              [chatProcess](QProcess::ProcessState state) {
                if(state == QProcess::NotRunning)
                  chatProcess->deleteLater();
              });
      connect(chatProcess,
              static_cast<void (QProcess::*)(int, QProcess::ExitStatus)>(&QProcess::finished),
              chatProcess,
              [chatProcess](int, QProcess::ExitStatus) { chatProcess->deleteLater(); });
      if(chatProcess->state() == QProcess::NotRunning)
        chatProcess->deleteLater();
    }
  }
  closeKillOnCloseJob(execution->jobHandle);
  clearChatContextFile(execution);
  clearChatCancelFile(execution);
  m_ChatExecutions.remove(targetTaskId);
  if(execution->startTimer)
    execution->startTimer->deleteLater();
  delete execution;
  if(!targetTaskId.isEmpty() && !m_CompletedTasks.contains(targetTaskId))
  {
    m_CompletedTasks.insert(targetTaskId);
    unwatchTask(targetTaskId);
    m_RunningTasks.remove(targetTaskId);
    const QString state = terminalState.isEmpty() ? lit("interrupted") : terminalState;
    addTaskActivity(targetTaskId, state,
                    state == lit("superseded") ? tr("Interrupted for new direction")
                                                : tr("Request stopped"),
                    QString(), true, false);
    setTaskState(targetTaskId, state, true);
    m_UserPrompts.remove(targetTaskId);
    m_TaskOffsets.remove(targetTaskId);
    m_TaskSequences.remove(targetTaskId);
    m_TaskTails.remove(targetTaskId);
    m_TaskTexts.remove(targetTaskId);
    m_TaskAgentMessageStreams.remove(targetTaskId);
    m_TaskConversationIds.remove(targetTaskId);
    m_TaskMessageIds.remove(targetTaskId);
  }
  if(refreshUI)
  {
    refreshConversationList();
    updateComposerState();
    scheduleConversationSave();
    refreshConnectionStatus();
    if(continueQueue)
      QTimer::singleShot(0, this, [this]() { startNextChat(); });
  }
}

void AIAssistant::clearCaptureTasks()
{
  const int taskCount = m_KnownTasks.size();
  const QStringList directChatTasks = m_ChatExecutions.keys();
  for(const QString &taskId : directChatTasks)
    requestChatCancellation(chatExecution(taskId));
  for(const QString &taskId : directChatTasks)
    // i18n-ignore: Stable internal task-state protocol value; never shown as UI text.
    cancelChat(lit("interrupted"), false, taskId, 0, false);
  discardAllChatContextFreezes();
  const qint64 interruptedMs = QDateTime::currentMSecsSinceEpoch();
  for(const PendingChat &request : m_ChatQueue)
  {
    ConversationThread *thread = conversation(request.conversationId);
    if(!thread || request.prompt.trimmed().isEmpty())
      continue;
    AIChatMessage item;
    item.id = lit("message-") + request.taskId;
    item.role = lit("user");
    item.text = request.prompt;
    item.taskId = request.taskId;
    item.state = lit("interrupted");
    item.timestampMs = interruptedMs;
    item.finishedTimestampMs = interruptedMs;
    AIActivityItem activity;
    activity.kind = lit("interrupted");
    activity.title = tr("Queued message was not run before the capture closed");
    activity.timestampMs = interruptedMs;
    activity.finishedTimestampMs = interruptedMs;
    activity.completed = true;
    item.activity.push_back(activity);
    thread->messages.push_back(item);
    thread->updatedMs = interruptedMs;
  }
  m_ChatQueue.clear();

  for(const QString &taskId : m_KnownTasks)
    m_CompletedTasks.insert(taskId);
  if(m_CompletedTasks.size() > 512)
  {
    m_CompletedTasks.clear();
    for(const QString &taskId : directChatTasks)
      if(!taskId.isEmpty())
        m_CompletedTasks.insert(taskId);
    for(const QString &taskId : m_KnownTasks)
      m_CompletedTasks.insert(taskId);
  }

  m_KnownTasks.clear();
  updateEventTimer();
  m_RunningTasks.clear();
  m_TaskOffsets.clear();
  m_TaskSequences.clear();
  m_TaskTails.clear();
  m_TaskTexts.clear();
  m_TaskAgentMessageStreams.clear();
  m_UserPrompts.clear();
  m_TaskConversationIds.clear();
  m_TaskMessageIds.clear();

  if(taskCount > 0)
    appendSystem(tr("Capture closed; discarded %1 capture-bound AI task(s).").arg(taskCount));
  saveConversations();
  updateComposerState();
}

void AIAssistant::finishChatProcess(const QString &taskId, int exitCode)
{
  ChatExecution *execution = chatExecution(taskId);
  if(!execution)
    return;
  if(execution->startTimer)
    execution->startTimer->stop();
  execution->pendingInput.clear();
  execution->promptSent = false;
  QProcess *process = execution->process;
  execution->process = NULL;
  if(process)
  {
    const QByteArray finalOutput = process->readAllStandardOutput();
    const QByteArray finalError = process->readAllStandardError();
    if(execution->stdoutData.size() < MaxProcessOutputBytes)
      execution->stdoutData +=
          finalOutput.left(MaxProcessOutputBytes - execution->stdoutData.size());
    if(execution->stderrData.size() < MaxEventLineBytes)
      execution->stderrData += finalError.left(MaxEventLineBytes - execution->stderrData.size());
    closeKillOnCloseJob(execution->jobHandle);
    process->deleteLater();
  }
  closeKillOnCloseJob(execution->jobHandle);
  updateComposerState();
  drainChatTaskEvents(taskId, exitCode, MaxEventDrainBytes);
}

void AIAssistant::drainChatTaskEvents(const QString &taskId, int exitCode,
                                      qint64 remainingBytes)
{
  if(taskId.isEmpty() || !chatExecution(taskId))
    return;
  if(m_CompletedTasks.contains(taskId))
  {
    finishChatResult(taskId, exitCode);
    return;
  }

  bool atEnd = false;
  // Process exit can leave thousands of small token events in the final disk chunk. Apply the
  // same parse/time budget as live polling; the byte limit alone does not bound GUI-thread work.
  m_TaskEventRecordsRemaining = MaxEventRecordsPerTick;
  m_TaskEventPollClock.start();
  m_TaskEventBudgetActive = true;
  const qint64 readBytes =
      pollTaskEvent(taskId, qMin(MaxEventReadBytes, remainingBytes), &atEnd);
  m_TaskEventBudgetActive = false;
  remainingBytes -= readBytes;
  if(m_CompletedTasks.contains(taskId))
  {
    finishChatResult(taskId, exitCode);
    return;
  }

  if(!atEnd && readBytes > 0 && remainingBytes > 0)
  {
    // Drain one bounded disk chunk per event-loop turn. Large legal answers can produce both
    // streamed deltas and a final reset stream; 16 MiB covers that protocol worst case without a
    // long synchronous UI stall.
    QTimer::singleShot(0, this, [this, taskId, exitCode, remainingBytes]() {
      drainChatTaskEvents(taskId, exitCode, remainingBytes);
    });
    return;
  }

  if(atEnd)
  {
    const QByteArray tail = m_TaskTails.take(taskId).trimmed();
    if(!tail.isEmpty() && tail.size() <= MaxEventLineBytes)
      consumeTaskEvent(taskId, tail);
  }
  finishChatResult(taskId, exitCode);
}

void AIAssistant::finishChatResult(const QString &taskId, int exitCode)
{
  ChatExecution *execution = chatExecution(taskId);
  if(taskId.isEmpty() || !execution)
    return;

  if(!taskId.isEmpty() && !m_CompletedTasks.contains(taskId))
  {
    const QJsonDocument document =
        QJsonDocument::fromJson(execution->stdoutData.left(MaxProcessOutputBytes));
    const QJsonObject resultObject = document.isObject() ? document.object() : QJsonObject();
    // i18n-ignore: ReplayLab CLI result JSON field name defined by the backend protocol.
    const QString result = resultObject.value(lit("text")).toString();
    QString terminalStatus;
    const bool validTerminal = normalizedTerminalStatus(resultObject, terminalStatus);
    // The TaskEvent stream is authoritative. This stdout branch is only a compatibility fallback
    // when a provider exits before qrenderdoc observes its terminal event.
    if(validTerminal && terminalStatus == lit("needs-user-input"))    // i18n-ignore: task lifecycle state
    {
      ClarificationPrompt clarification;
      // i18n-ignore: ReplayLab result JSON field name defined by the backend protocol.
      if(clarificationFromJSON(resultObject.value(lit("clarification")).toObject(), taskId,
                               clarification))
      {
        bindClarificationOrigin(clarification, taskId);
        if(ConversationThread *thread = conversation(m_TaskConversationIds.value(taskId)))
          thread->clarification = clarification;
      }
      else
      {
        // Same as the task-event path: "Waiting for input" with no question and no
        // buttons leaves the user with nothing to act on and no idea why.
        appendSystem(tr("The AI is waiting for an answer, but its question could not "
                        "be displayed. Reply in your own words to continue."),
                     true);
      }
    }
    if(exitCode == 0 && validTerminal)
      finishTask(taskId, terminalStatus, result);
    else if(exitCode == 0 && !result.isEmpty())
      // i18n-ignore: Task lifecycle state identifier used only for control flow.
      finishTask(taskId, lit("failed"), tr("AI result contained an invalid terminal status."));
    else
    {
      const QString diagnostic =
          QString::fromUtf8(execution->stderrData.left(MaxEventLineBytes)).trimmed();
      if(!diagnostic.isEmpty())
        qWarning() << QCoreApplication::translate("AIAssistant", "Bread AI task ended without a validated terminal result:")
                   << diagnostic;
      QString failure = tr("AI task failed before a validated terminal result was received. Check the "
                           "diagnostic log for technical details.");
      // Display only the CLI's fixed category, never stderr/provider text which can contain
      // credentials or attachment contents. Early envelope failures have no TaskEvent stream.
      if(diagnostic.contains(QLatin1String("replaylab-ai: error[invalid-request]:")))
        failure = tr("The AI request or its attachments were invalid, so the Agent did not start. "
                     "Attach the selection again and retry.");
      else if(diagnostic.contains(QLatin1String("replaylab-ai: error[local-io-failure]:")))
        failure = tr("The Agent could not read or write a required local file. Check the diagnostic "
                     "log for the affected file.");
      // i18n-ignore: serialized task state; failure contains the translated user message.
      finishTask(taskId, lit("failed"), failure);
    }
  }
  clearChatContextFile(execution);
  clearChatCancelFile(execution);
  m_ChatExecutions.remove(taskId);
  if(execution->startTimer)
    execution->startTimer->deleteLater();
  delete execution;
  updateComposerState();
  refreshConnectionStatus();
  QTimer::singleShot(0, this, [this]() { startNextChat(); });
}

void AIAssistant::watchTask(const QString &taskId, const QString &userPrompt,
                            const QString &conversationId, const QString &messageId)
{
  if(m_DeletedTasks.contains(taskId))
    return;
  if(!safeTaskId(taskId))
  {
    appendSystem(tr("Ignored an invalid AI task identifier."), true);
    return;
  }
  m_KnownTasks.insert(taskId);
  updateEventTimer();
  m_RunningTasks.insert(taskId);
  m_TaskOffsets[taskId] = 0;
  m_TaskSequences[taskId] = 0;
  m_TaskTails.remove(taskId);
  m_TaskTexts.remove(taskId);
  m_TaskAgentMessageStreams.remove(taskId);
  if(!userPrompt.isEmpty())
    m_UserPrompts[taskId] = userPrompt;
  if(!conversationId.isEmpty())
    m_TaskConversationIds[taskId] = conversationId;
  if(!messageId.isEmpty())
    m_TaskMessageIds[taskId] = messageId;
}

void AIAssistant::unwatchTask(const QString &taskId)
{
  m_KnownTasks.remove(taskId);
  updateEventTimer();
}

void AIAssistant::updateEventTimer()
{
  if(!m_EventTimer)
    return;

  if(!m_ShuttingDown && !m_KnownTasks.isEmpty())
  {
    if(!m_EventTimer->isActive())
      m_EventTimer->start();
  }
  else if(m_EventTimer->isActive())
  {
    m_EventTimer->stop();
  }
}

void AIAssistant::ReportError(const QString &message)
{
  appendSystem(message.isEmpty() ? tr("The Bread AI action failed without a message.") : message,
               true);
}

void AIAssistant::RebindCaptureFile(const QString &capturePath)
{
  const QString normalized = AIConversationStore::NormalizedCapturePath(capturePath);
  if(normalized.isEmpty() ||
     normalized == AIConversationStore::NormalizedCapturePath(m_CaptureConversationPath))
    return;

  // Retain the old key as a recoverable copy, then atomically write the same in-memory history
  // under the new RDC key. Future mutations use only the new path.
  saveConversations();
  m_CaptureConversationPath = capturePath;
  saveConversations();
}

void AIAssistant::pollTaskEvents()
{
  QStringList tasks = m_KnownTasks.values();
  if(tasks.isEmpty())
  {
    m_TaskEventPollCursor = 0;
    return;
  }

  // These are global per-tick budgets, not per-task budgets. Otherwise each additional background
  // task could multiply filesystem reads and JSON parsing on the GUI thread every 50ms. Rotate the
  // first task so one busy event stream cannot starve its peers. pollTaskEvent() retains any
  // complete records left after the record/time budget, so stopping a tick never drops an event.
  std::sort(tasks.begin(), tasks.end());
  const int start = m_TaskEventPollCursor % tasks.count();
  qint64 remainingBytes = MaxEventReadBytes;
  m_TaskEventRecordsRemaining = MaxEventRecordsPerTick;
  m_TaskEventPollClock.start();
  m_TaskEventBudgetActive = true;
  int visited = 0;
  while(visited < tasks.count() && remainingBytes > 0 &&
        (visited == 0 || (m_TaskEventRecordsRemaining > 0 &&
                         m_TaskEventPollClock.elapsed() < MaxEventPollTimeMs)))
  {
    const QString &taskId = tasks[(start + visited) % tasks.count()];
    if(m_KnownTasks.contains(taskId))
      remainingBytes -= qMin(remainingBytes, pollTaskEvent(taskId, remainingBytes));
    ++visited;
  }
  m_TaskEventBudgetActive = false;
  m_TaskEventPollCursor = (start + visited) % tasks.count();
}

qint64 AIAssistant::pollTaskEvent(const QString &taskId, qint64 maxReadBytes, bool *atEnd)
{
  if(atEnd)
    *atEnd = true;
  if(!safeTaskId(taskId) || maxReadBytes <= 0)
    return 0;

  QFile file(QDir(taskEventRoot()).filePath(taskId + lit(".jsonl")));
  if(!file.open(QIODevice::ReadOnly))
    return 0;
  qint64 offset = m_TaskOffsets.value(taskId, 0);
  if(file.size() < offset)
  {
    offset = 0;
    m_TaskTails.remove(taskId);
    m_TaskSequences[taskId] = 0;
  }
  if(!file.seek(offset))
    return 0;

  QByteArray pending = m_TaskTails.value(taskId);
  QByteArray chunk;
  // If the previous tick stopped with complete records buffered, consume those before advancing
  // the file offset again. This bounds per-task memory even when a single event is expensive.
  if(!pending.contains('\n'))
  {
    const qint64 available = qMax<qint64>(0, file.size() - offset);
    chunk = file.read(qMin(qMin(maxReadBytes, MaxEventReadBytesPerTask), available));
    m_TaskOffsets[taskId] = file.pos();
    pending += chunk;
  }
  int lineStart = 0;
  int newline = -1;
  while((newline = pending.indexOf('\n', lineStart)) >= 0)
  {
    // Allow the first record even if opening/reading the file consumed the time budget. A final
    // process drain must make progress through a buffered chunk before yielding to the GUI.
    if(m_TaskEventBudgetActive &&
       (m_TaskEventRecordsRemaining <= 0 ||
        (lineStart > 0 && m_TaskEventPollClock.elapsed() >= MaxEventPollTimeMs)))
      break;
    const QByteArray line = pending.mid(lineStart, newline - lineStart).trimmed();
    lineStart = newline + 1;
    if(!line.isEmpty())
    {
      if(m_TaskEventBudgetActive)
        --m_TaskEventRecordsRemaining;
      if(line.size() <= MaxEventLineBytes)
        consumeTaskEvent(taskId, line);
    }
    if(!m_KnownTasks.contains(taskId))
      break;
  }
  if(lineStart > 0)
    pending = pending.mid(lineStart);
  // A large buffer is expected when a tick deliberately stops before complete records. Only an
  // unterminated record exceeding the protocol line limit is malformed.
  if(!pending.contains('\n') && pending.size() > MaxEventLineBytes)
  {
    // A background conversation may fail its event protocol while the user is
    // viewing another chat. Keep the diagnostic with the task that produced it.
    appendSystem(tr("AI task event exceeded the safe line limit."), true,
                 m_TaskConversationIds.value(taskId));
    pending.clear();
  }
  if(m_KnownTasks.contains(taskId))
    m_TaskTails[taskId] = pending;
  if(atEnd)
    *atEnd = file.pos() >= file.size() && !pending.contains('\n');
  // A process-exit drain uses a positive result to schedule another event-loop turn. Report one
  // byte of logical progress when this call consumed only a previously buffered record, while the
  // periodic global I/O budget still charges newly read bytes exactly once.
  return chunk.isEmpty() && lineStart > 0 ? 1 : chunk.size();
}

void AIAssistant::consumePublicAgentMessageEvent(const QString &taskId, const QJsonObject &event,
                                                 const QJsonObject &eventData,
                                                 qint64 timestampMs)
{
  if(!taskMessage(taskId))
    return;

  // i18n-ignore: Stable ReplayLab task-event protocol fields.
  const QString agentId = eventData.value(QLatin1String("agentId")).toString().left(512);
  const QString planTaskId = eventData.value(QLatin1String("planTaskId")).toString().left(256);
  const QString declaredStream = eventData.value(QLatin1String("stream")).toString().left(64);
  const QString itemId = eventData.value(QLatin1String("itemId")).toString().left(256);
  const QString messagePhase = eventData.value(QLatin1String("phase")).toString().left(64);
  const QString identity = agentId.isEmpty() ? planTaskId : agentId;
  if(identity.isEmpty())
    return;

  // Keep provider retries and distinct message items isolated. The separator is internal and is
  // never rendered or serialized into conversation history.
  const QString streamKey = identity + QChar(0x1e) + declaredStream + QChar(0x1e) + itemId;
  QHash<QString, LiveAgentMessageStream> &streams = m_TaskAgentMessageStreams[taskId];
  if(!streams.contains(streamKey) && streams.size() >= MaxLiveAgentMessageStreams)
  {
    // Dropping the new one used to mean the activity group went permanently silent
    // partway through a long run -- which reads as a hung task, while the run is
    // still working. The cap is about memory, not about which messages matter, so
    // retire the stream that has been idle longest and keep showing progress.
    QString oldest;
    qint64 oldestMs = 0;
    for(auto it = streams.constBegin(); it != streams.constEnd(); ++it)
    {
      if(oldest.isEmpty() || it.value().touchedMs < oldestMs)
      {
        oldestMs = it.value().touchedMs;
        oldest = it.key();
      }
    }
    if(oldest.isEmpty())
      return;
    streams.remove(oldest);
  }
  LiveAgentMessageStream &stream = streams[streamKey];
  stream.touchedMs = timestampMs;

  // i18n-ignore: Stable ReplayLab task-event protocol fields.
  const QString role = eventData.value(QLatin1String("agentRole")).toString();
  const QString skill = eventData.value(QLatin1String("skill")).toString().left(256);
  const bool commentary = messagePhase == QLatin1String("commentary") && !itemId.isEmpty();
  const QString activityKind =
      commentary ? QLatin1String("progressNote") : QLatin1String("child-message");

  const QString delta = event.value(QLatin1String("text")).toString();
  // i18n-ignore: Stable ReplayLab task-event protocol field.
  const bool reset = eventData.value(QLatin1String("reset")).toBool();
  const QString previousProjection = stream.projectedText;
  bool newlyOmitted = false;

  // A reset snapshot can span several TaskEvents. Assemble it away from the visible stream, then
  // compare once at the atomic completion boundary. This prevents an identical 9K+ authoritative
  // receipt from clearing and replaying text that the provider already streamed.
  // i18n-ignore: Stable ReplayLab atomic-replacement protocol fields.
  const QString replacementId =
      eventData.value(QLatin1String("replacementId")).toString().left(128);
  if(!replacementId.isEmpty())
  {
    // i18n-ignore: Stable ReplayLab atomic-replacement protocol fields.
    const int chunkIndex =
        eventData.value(QLatin1String("replacementChunkIndex")).toInt(-1);
    const int replacementBytes =
        eventData.value(QLatin1String("replacementBytes")).toInt(-1);
    const bool replacementComplete =
        eventData.value(QLatin1String("replacementComplete")).toBool();
    if(reset)
    {
      if(chunkIndex != 0 || !publicAgentReplacementIdIsValid(replacementId) ||
         replacementBytes <= 0 || replacementBytes > MaxLiveAgentMessageChars * 4)
        return;
      stream.replacementId = replacementId;
      stream.replacementText.clear();
      stream.replacementChunks = 0;
      stream.replacementBytes = replacementBytes;
    }
    if(stream.replacementId != replacementId || chunkIndex != stream.replacementChunks ||
       stream.replacementBytes != replacementBytes)
      return;
    if(stream.replacementText.size() + delta.size() > MaxLiveAgentMessageChars)
    {
      stream.replacementId.clear();
      stream.replacementText.clear();
      stream.replacementChunks = 0;
      stream.replacementBytes = -1;
      newlyOmitted = !stream.omitted;
      stream.omitted = true;
    }
    else
    {
      stream.replacementText += delta;
      ++stream.replacementChunks;
      if(!replacementComplete)
        return;

      const QString replacement = stream.replacementText;
      const QByteArray replacementUtf8 = replacement.toUtf8();
      const QString actualReplacementId = QString::fromLatin1(
          QCryptographicHash::hash(replacementUtf8, QCryptographicHash::Sha256).toHex());
      const bool validReplacement = replacementUtf8.size() == stream.replacementBytes &&
                                    actualReplacementId.compare(replacementId,
                                                                Qt::CaseInsensitive) == 0;
      stream.replacementId.clear();
      stream.replacementText.clear();
      stream.replacementChunks = 0;
      stream.replacementBytes = -1;
      if(!validReplacement)
        return;
      if(replacement != stream.sourceText)
      {
        stream.sourceText = replacement;
        stream.receivedChars = replacement.size();
        stream.omitted = false;
      }
    }
  }
  else
  {
    // An unrelated ordinary delta proves that an incomplete replacement batch was abandoned.
    stream.replacementId.clear();
    stream.replacementText.clear();
    stream.replacementChunks = 0;
    stream.replacementBytes = -1;

    // Backward compatibility for older single-event reset producers.
    const bool redundantReset = reset && delta == stream.sourceText;
    const bool replaceSource = reset && !redundantReset;
    if(replaceSource)
    {
      stream.sourceText.clear();
      stream.receivedChars = 0;
      stream.omitted = false;
    }
    const bool wasOmitted = stream.omitted;
    const int available = qMax(0, MaxLiveAgentMessageChars - stream.receivedChars);
    const QString accepted = redundantReset || stream.omitted ? QString() : delta.left(available);
    stream.sourceText += accepted;
    stream.receivedChars += accepted.size();
    if(!redundantReset && accepted.size() < delta.size())
      stream.omitted = true;
    newlyOmitted = !wasOmitted && stream.omitted;
  }

  // Commentary is already an explicit public message and must remain byte-for-byte at the QString
  // layer, even when a sentence happens to begin with a brace. Other provider observations can be
  // structured control contracts and therefore use the schema-qualified narrative projector.
  // A regular chat Agent can emit an exact final_answer item. Other non-commentary provider
  // observations remain fail-closed until schema-qualified narrative values arrive, even if the
  // provider prepends a sentence or an unlabelled Markdown fence before its JSON object.
  const bool structuredObservation = !commentary && role != QLatin1String("chat");
  const QString projected = commentary || !structuredObservation
                                ? stream.sourceText
                                : publicAgentMessageProjection(stream.sourceText, NULL, true);
  QString visibleDelta;
  bool visibleReset = false;
  if(!stream.activityCreated)
  {
    visibleDelta = projected;
  }
  else if(projected.startsWith(previousProjection))
  {
    visibleDelta = projected.mid(previousProjection.size());
  }
  else
  {
    visibleDelta = projected;
    visibleReset = true;
  }
  stream.projectedText = projected;

  QString activityId = eventData.value(QLatin1String("activityId")).toString();
  if(commentary)
    activityId = (identity + QLatin1Char(':') + itemId).left(512);
  else if(activityId.isEmpty())
    activityId = QLatin1String("agent-message:") + identity + QLatin1Char(':') + declaredStream +
                 QLatin1Char(':') + itemId;
  QString title = commentary ? tr("Progress update") : tr("Agent working");
  if(!skill.isEmpty())
    title = commentary ? tr("Progress update") : tr("Child Agent: %1").arg(skill);

  // i18n-ignore: Stable ReplayLab task-event lifecycle values.
  const QString lifecycle = eventData.value(QLatin1String("activityLifecycle")).toString();
  const bool completed = lifecycle == QLatin1String("completed") ||
                         lifecycle == QLatin1String("failed") ||
                         lifecycle == QLatin1String("cancelled") ||
                         lifecycle == QLatin1String("canceled");
  const bool failed = lifecycle == QLatin1String("failed");
  // Commentary has an earlier cumulative progress snapshot with the same activity ID. Its first
  // exact message must take text ownership even when the provider did not mark that delta reset.
  visibleReset |= commentary && !stream.activityCreated;
  if(!visibleDelta.isEmpty() || visibleReset || (completed && stream.activityCreated))
  {
    // Ordinary prose is passed through unchanged. Structured provider output is reduced to
    // schema-qualified narrative values while its JSON syntax, function names, hashes and route
    // metadata stay in the Agent Monitor. The prefix-tolerant projector makes both paths grow
    // monotonically, so the view can reveal one grapheme without waiting for a complete document.
    appendTaskActivityText(taskId, activityKind, title, visibleDelta, visibleReset, completed,
                           failed, timestampMs, activityId);
    stream.activityCreated = true;
    stream.activityId = activityId;
    stream.activityKind = activityKind;
  }

  if(newlyOmitted)
  {
    // This is a protocol safety fallback, not a silent UI truncation. Normal ReplayLab producers
    // apply the identical 1 Mi-character bound before writing TaskEvents, so valid streams never
    // reach this branch.
    addTaskActivity(taskId, lit("interrupted"), tr("Agent working"),    // i18n-ignore: internal activity-kind ID
                    tr("Agent output is too large for the live conversation. Open the Agent "
                       "Monitor for the complete trace."),
                    true, true, timestampMs, activityId + lit(":limit"));    // i18n-ignore: internal activity-ID suffix
  }

  if(completed)
  {
    streams.remove(streamKey);
    if(streams.isEmpty())
      m_TaskAgentMessageStreams.remove(taskId);
  }
}

void AIAssistant::consumeTaskEvent(const QString &taskId, const QByteArray &line)
{
  if(m_DeletedTasks.contains(taskId))
    return;
  QJsonParseError parseError;
  const QJsonDocument document = QJsonDocument::fromJson(line, &parseError);
  if(parseError.error != QJsonParseError::NoError || !document.isObject())
    return;
  const QJsonObject event = document.object();
  if(event.value(lit("schema")).toString() != lit("replaylab-ai-task-event/v1") ||
     event.value(lit("taskId")).toString() != taskId)
    return;
  const qint64 sequence = event.value(lit("sequence")).toVariant().toLongLong();
  if(sequence <= m_TaskSequences.value(taskId, 0))
    return;
  m_TaskSequences[taskId] = sequence;

  // The Agent monitor owns a separate local-observability model. Reasoning and child-Agent draft
  // events are consumed here before the ordinary transcript intentionally filters them out.
  if(m_AgentTraceStore)
    m_AgentTraceStore->ConsumeEvent(event);

  const QString kind = event.value(lit("kind")).toString();
  const QString summary = event.value(lit("summary")).toString();
  const QJsonObject eventData = event.value(lit("data")).toObject();
  // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
  qint64 eventTimestampMs = event.value(lit("timestampUnixMs")).toVariant().toLongLong();
  if(eventTimestampMs <= 0)
    eventTimestampMs = QDateTime::currentMSecsSinceEpoch();
  if(kind == lit("queued"))
  {
    // A queued event starts a fresh attempt. Discard every provisional delta from the previous
    // attempt before accepting the new stream.
    m_TaskTexts.remove(taskId);
    m_TaskAgentMessageStreams.remove(taskId);
    setTaskState(taskId, lit("queued"), false);
    // i18n-ignore: Stable ReplayLab task-activity protocol fields.
    const QString title = localizeTaskRuntimeMessage(
        // i18n-ignore: Stable ReplayLab task-activity protocol field.
        eventData.value(lit("activityTitleId")).toString(),
        // i18n-ignore: Stable ReplayLab task-activity protocol field.
        eventData.value(lit("activityTitleArgs")).toArray(), tr("Queued"));
    QStringList details;
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const QString backend = event.value(lit("backend")).toString();
    if(!backend.isEmpty())
      details.push_back(tr("Backend: %1").arg(backend));
    // i18n-ignore: ReplayLab task-event JSON field names defined by the backend protocol.
    QString connection = eventData.value(lit("profileTitle")).toString().trimmed();
    if(connection.isEmpty())
      // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
      connection = event.value(lit("profile")).toString();
    if(!connection.isEmpty())
      details.push_back(tr("Connection: %1").arg(connection));
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const QString model = eventData.value(lit("model")).toString().trimmed();
    if(!model.isEmpty())
      details.push_back(tr("Model: %1").arg(model));
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const int attempt = eventData.value(lit("attempt")).toInt();
    if(attempt > 1)
      details.push_back(tr("Attempt: %1").arg(attempt));
    // i18n-ignore: Newline is a presentation separator, not user-visible source text.
    const QString detail = details.isEmpty() ? summary : details.join(lit("\n"));
    // i18n-ignore: Activity kind is a backend protocol identifier; its visible title is translated.
    addTaskActivity(taskId, lit("queued"), title, detail.left(4000), false, false,
                    eventTimestampMs);
    setDiagnosticStatus(m_Status, DiagnosticLevel::Info, tr("Queued..."));
  }
  else if(kind == QLatin1String("message-delta") ||
          kind == QLatin1String("agent-message-delta"))
  {
    // deepagents sub-agents may stream concurrently. Their draft text is supporting evidence, not
    // the authoritative assistant reply. Project each public stream into its own activity row so
    // concurrent sub-agents cannot interleave or masquerade as the final answer.
    const bool childDraft = kind == QLatin1String("agent-message-delta") ||
                            (!eventData.value(QLatin1String("planTaskId")).toString().isEmpty() &&
                             eventData.value(QLatin1String("stream")).toString() !=
                                 QLatin1String("final"));
    if(childDraft)
    {
      consumePublicAgentMessageEvent(taskId, event, eventData, eventTimestampMs);
      return;
    }
    // Keep a single authoritative live buffer whenever the transcript row still exists. Mirroring
    // it into m_TaskTexts after every delta makes the two QStrings share storage; the next append
    // must then detach and copy the whole accumulated answer. Long streamed answers turned that
    // into quadratic memory traffic on the GUI thread.
    AIChatMessage *item = taskMessage(taskId);
    QString &answer = item ? item->provisionalText : m_TaskTexts[taskId];
    if(eventData.value(lit("reset")).toBool())
      answer.clear();
    if(answer.size() < MaxAnswerBytes)
      answer += event.value(lit("text")).toString().left(MaxAnswerBytes - answer.size());
    if(item)
    {
      item->state = lit("running");
      item->activityExpanded = true;
      // A single poll can contain many token events. Coalesce full QTextDocument rebuilds to
      // keep long conversations responsive while still presenting live output at ~16 fps.
      scheduleConversationRender();
    }
  }
  // i18n-ignore: Activity kind is a stable ReplayLab task-event protocol identifier.
  else if(kind == lit("reasoning-delta"))
  {
    // Only provider-exposed text is projected. Keep raw and summary streams separate, including
    // their item/index identities and resets. Opaque/encrypted events remain monitor metadata.
    const QString activityKind = AIAgentTraceStore::ConversationReasoningKind(eventData);
    if(activityKind.isEmpty())
      return;
    // i18n-ignore: Reasoning lifecycle values are stable task-event protocol identifiers.
    const QString lifecycle = eventData.value(lit("lifecycle")).toString();
    // i18n-ignore: Lifecycle values are stable ReplayLab task-event protocol identifiers.
    if(lifecycle == lit("started") || lifecycle == lit("updated"))
      setDiagnosticStatus(m_Status, DiagnosticLevel::Info, tr("Receiving reasoning..."));
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const bool childReasoning = !eventData.value(lit("planTaskId")).toString().isEmpty();
    const QString text = event.value(lit("text")).toString();    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    // i18n-ignore: Lifecycle values are stable ReplayLab task-event protocol identifiers.
    const bool finished = lifecycle == lit("completed") || lifecycle == lit("truncated") ||
                          lifecycle == lit("failed") || lifecycle == lit("cancelled");    // i18n-ignore: AgentHost protocol identifier, not UI prose.
    if(!childReasoning && (!text.isEmpty() || finished))
    {
      const QString activityId = AIAgentTraceStore::ConversationReasoningId(eventData);
      if(activityKind == lit("reasoning"))    // i18n-ignore: summary activity kind.
      {
        AIChatMessage *taskItem = taskMessage(taskId);
        const QString providerItemId = eventData.value(lit("itemId")).toString();    // i18n-ignore: provider item identity.
        if(taskItem && !providerItemId.isEmpty())
          for(int i = taskItem->activity.size() - 1; i >= 0; --i)
            if(taskItem->activity[i].kind == activityKind &&
               taskItem->activity[i].id == providerItemId &&
               !taskItem->activity[i].exactText && taskItem->activity[i].detail.isEmpty())
              taskItem->activity.remove(i);
      }
      int received = 0;
      const bool reset = eventData.value(lit("reset")).toBool();    // i18n-ignore: task stream field.
      if(!reset)
      {
        const AIChatMessage *taskItem = taskMessage(taskId);
        if(taskItem)
          for(const AIActivityItem &activity : taskItem->activity)
            if(activity.kind == activityKind && activity.id == activityId)
              received = activity.detail.size();
      }
      QString visible = text;
      if(received >= MaxLiveReasoningChars)
        visible.clear();
      else if(received + visible.size() > MaxLiveReasoningChars)
        visible = visible.left(MaxLiveReasoningChars - received) + lit("\n") +
                  tr("[reasoning continues in the Agent monitor]");
      if(!visible.isEmpty() || finished)
        appendTaskActivityText(taskId, activityKind,
                               // i18n-ignore: Backend activity channel discriminator.
                               activityKind == lit("reasoning") ? tr("Reasoning summary")
                                                                 : tr("Raw reasoning"),
                               // i18n-ignore: Backend activity lifecycle discriminator.
                               visible, reset, finished, lifecycle == lit("failed"),
                               eventTimestampMs, activityId);
    }
  }
  else if(kind == lit("tool-started"))
  {
    const QString tool = event.value(lit("tool")).toString();
    QString detail = tr("Running");
    QStringList argumentNames;
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    for(const QJsonValue &name : eventData.value(lit("argumentNames")).toArray())
      if(name.isString() && !name.toString().trimmed().isEmpty())
        argumentNames.push_back(name.toString().trimmed().left(128));
    if(!argumentNames.isEmpty())
      detail += lit("\n") + tr("Arguments: %1").arg(argumentNames.join(lit(", ")));
    // i18n-ignore: Activity kind is a backend protocol identifier; the tool name is displayed separately.
    addTaskActivity(taskId, lit("tool"), tr("Tool: %1").arg(tool), detail.left(4000), false,
                    false,
                    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
                    eventTimestampMs, eventData.value(lit("activityId")).toString());
  }
  else if(kind == lit("tool-completed"))
  {
    const QString tool = event.value(lit("tool")).toString();
    const bool failed = eventData.value(lit("status")).toString() == lit("failed");
    QString detail = failed ? tr("Failed") : tr("Completed");
    // i18n-ignore: structured task-event diagnostic field, never translated.
    const QJsonValue elapsedMs = eventData.value(lit("elapsedMs"));
    if(elapsedMs.isDouble())
      detail += lit("\n") + tr("Tool round trip: %1 ms").arg(elapsedMs.toInt());
    // i18n-ignore: redacted backend task-event diagnostic field.
    const QString diagnostic = eventData.value(lit("publicDetail")).toString().left(400);
    if(failed && !diagnostic.isEmpty())
      detail += lit("\n") + diagnostic;
    // i18n-ignore: Activity kind is a backend protocol identifier; the tool name is displayed separately.
    addTaskActivity(taskId, lit("tool"), tr("Tool: %1").arg(tool), detail, true, failed,
                    eventTimestampMs,
                    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
                    eventData.value(lit("activityId")).toString());
  }
  else if(kind == lit("running"))
  {
    setTaskState(taskId, lit("running"), false);
    const QString phase = eventData.value(lit("phase")).toString();
    const QString planTaskId = eventData.value(lit("planTaskId")).toString();
    const QString skill = eventData.value(lit("skill")).toString();
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const QString explicitActivityId = eventData.value(lit("activityId")).toString();
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const QString fallbackTitle = eventData.value(lit("activityTitle")).toString();
    // i18n-ignore: Stable ReplayLab task-activity protocol fields.
    const QString titleMessageId = eventData.value(lit("activityTitleId")).toString();
    QString title = localizeTaskRuntimeMessage(
        titleMessageId,
        // i18n-ignore: Stable ReplayLab task-activity protocol field.
        eventData.value(lit("activityTitleArgs")).toArray(), fallbackTitle);
    if(title.isEmpty())
      // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
      title = eventData.value(lit("activitySummary")).toString();
    if(title.isEmpty())
      title = summary;
    if(title.isEmpty())
      title = tr("Agent working");
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const QString fallbackDetail = eventData.value(lit("activityDetail")).toString();
    // i18n-ignore: Stable ReplayLab task-activity protocol fields.
    const QString detailMessageId = eventData.value(lit("activityDetailId")).toString();
    QString detail = localizeTaskRuntimeMessage(
        detailMessageId,
        // i18n-ignore: Stable ReplayLab task-activity protocol field.
        eventData.value(lit("activityDetailArgs")).toArray(), fallbackDetail);
    if(detail.isEmpty() && titleMessageId.isEmpty())
      detail = skill;
    const int current = eventData.value(lit("progressCurrent")).toInt();
    const int total = eventData.value(lit("progressTotal")).toInt();
    if(total > 0)
      detail += (detail.isEmpty() ? QString() : lit(" | ")) +
                tr("%1/%2").arg(current).arg(total);
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    QString activityKind = eventData.value(lit("activityKind")).toString();
    if(activityKind.isEmpty())
      // i18n-ignore: Default activity kind is a backend protocol identifier.
      activityKind = phase.isEmpty() ? lit("running") : phase;
    // A public activity is prose, not a wire payload. If a provider sends structured data in the
    // fallback detail field, keep the legacy narrative projection for bounded diagnostic rows.
    // Live reasoning summaries and progress messages are already explicit public text: preserve
    // them exactly and let the conversation view coalesce their streamed suffixes for display.
    if(!isLiveTextActivityKind(activityKind))
    {
      bool structuredDetail = false;
      const QString projectedDetail = publicAgentMessageProjection(detail, &structuredDetail);
      if(structuredDetail)
        detail = projectedDetail;
    }
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const QString activityStatus = eventData.value(lit("status")).toString();
    // i18n-ignore: Task-event status identifier used only for control flow.
    const bool failed = activityStatus == lit("failed");
    // i18n-ignore: Task-event status identifier used only for control flow.
    const bool completed = activityStatus == lit("completed") || failed ||
                           // i18n-ignore: Task-event status identifier used only for control flow.
                           activityStatus == lit("declined") ||
                           // i18n-ignore: Task-event status identifier used only for control flow.
                           activityStatus == lit("cancelled");
    if(!planTaskId.isEmpty() && explicitActivityId.isEmpty())
      activityKind += lit("-") + planTaskId;
    const QString activityId = explicitActivityId.isEmpty() ? planTaskId : explicitActivityId;
    // Codex also emits a cumulative compatibility snapshot for an exact summary stream. Its
    // per-part deltas own the visible text; rendering the snapshot too duplicates every summary.
    // i18n-ignore: Backend activity channel discriminator.
    if(activityKind == lit("reasoning") && !activityId.isEmpty())
    {
      const AIChatMessage *taskItem = taskMessage(taskId);
      const QString prefix = AIAgentTraceStore::ConversationSummaryPrefix(activityId);
      if(taskItem)
        for(const AIActivityItem &activity : taskItem->activity)
          if(activity.exactText && activity.kind == activityKind && activity.id.startsWith(prefix))
            return;
    }
    const int detailLimit =
        isLiveTextActivityKind(activityKind) ? MaxLiveAgentMessageChars : 4000;
    const QString boundedActivityKind = activityKind.left(64);
    bool exactAgentMessageActivity = false;
    const AIChatMessage *taskItem = taskMessage(taskId);
    if(taskItem && !activityId.isEmpty())
    {
      const QString boundedActivityId = activityId.left(MaxActivityIdChars);
      for(int i = taskItem->activity.size() - 1; i >= 0; --i)
      {
        const AIActivityItem &activity = taskItem->activity[i];
        if(activity.exactText && activity.id == boundedActivityId &&
           activity.kind == boundedActivityKind)
        {
          exactAgentMessageActivity = true;
          break;
        }
      }
    }

    // Codex publishes each commentary item twice: an exact delta for the Messages trace and a
    // sanitized cumulative running snapshot for generic clients. Once the exact stream has taken
    // ownership, keep its text byte-for-byte and use later snapshots only for lifecycle metadata.
    const bool lifecycleUpdated =
        exactAgentMessageActivity &&
        updateTaskActivityLifecycle(taskId, boundedActivityKind, title.left(1000), completed,
                                    failed, eventTimestampMs, activityId);
    if(!lifecycleUpdated)
      addTaskActivity(taskId, boundedActivityKind, title.left(1000), detail.left(detailLimit),
                      completed, failed, eventTimestampMs, activityId);
    else if(completed)
    {
      auto activeStreams = m_TaskAgentMessageStreams.find(taskId);
      if(activeStreams != m_TaskAgentMessageStreams.end())
      {
        QStringList completedStreamKeys;
        for(auto stream = activeStreams.value().constBegin();
            stream != activeStreams.value().constEnd(); ++stream)
        {
          if(stream.value().activityId == activityId &&
             stream.value().activityKind == boundedActivityKind)
            completedStreamKeys.push_back(stream.key());
        }
        for(const QString &completedStreamKey : completedStreamKeys)
          activeStreams.value().remove(completedStreamKey);
        if(activeStreams.value().isEmpty())
          m_TaskAgentMessageStreams.erase(activeStreams);
      }
    }
    setDiagnosticStatus(m_Status, DiagnosticLevel::Info, tr("Thinking..."));
  }
  else if(kind == lit("approval-required"))
  {
    addTaskActivity(taskId, lit("approval"), tr("Approval required"),
                    summary.isEmpty() ? tr("This action requires approval.") : summary, false,
                    true, eventTimestampMs);
  }
  // i18n-ignore: Task-event kinds are protocol identifiers used only for control flow.
  else if(kind == lit("completed") || kind == lit("partial") || kind == lit("failed") ||
          // i18n-ignore: Task-event kind is a protocol identifier used only for control flow.
          kind == lit("cancelled"))
  {
    // i18n-ignore: Task-event kinds are protocol identifiers used only for control flow.
    if(kind == lit("completed") || kind == lit("partial"))
    {
      QString terminalStatus;
      // A direct `partial` event is accepted for compatibility with early producers. Current
      // task streams use kind=completed with terminalStatus=partial.
      // i18n-ignore: Task lifecycle state and task-event field are protocol identifiers.
      bool validTerminal = kind == lit("partial") && !eventData.contains(lit("terminalStatus"));
      if(validTerminal)
        // i18n-ignore: Task lifecycle state identifier used only for control flow.
        terminalStatus = lit("partial");
      else
        validTerminal = normalizedTerminalStatus(eventData, terminalStatus);
      if(validTerminal)
      {
        if(terminalStatus == lit("needs-user-input"))    // i18n-ignore: task lifecycle state
        {
          ClarificationPrompt clarification;
          // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
          if(clarificationFromJSON(eventData.value(lit("clarification")).toObject(), taskId,
                                   clarification))
          {
            bindClarificationOrigin(clarification, taskId);
            if(ConversationThread *thread = conversation(m_TaskConversationIds.value(taskId)))
              thread->clarification = clarification;
          }
          else
          {
            // The run is waiting for an answer and the question could not be shown.
            // Silently rendering "Waiting for input" with no question and no buttons
            // leaves the user with nothing to act on and no idea why.
            appendSystem(tr("The AI is waiting for an answer, but its question could not "
                            "be displayed. Reply in your own words to continue."),
                         true);
          }
        }
        finishTask(taskId, terminalStatus, summary, eventTimestampMs);
        // The producer bounds `summary` to 1000 characters and `error` to 4000; a
        // provider stack trace or a multi-line auth diagnostic is cut before the part
        // that says what to do, and the rest was only in the raw JSONL.
        const QString detail = eventData.value(lit("error")).toString();    // i18n-ignore: backend protocol field
        if(!detail.isEmpty() && detail.size() > summary.size() && !detail.startsWith(summary))
          appendSystem(detail, true);
      }
      else
        // i18n-ignore: Task lifecycle state identifier used only for control flow.
        finishTask(taskId, lit("failed"), tr("AI result contained an invalid terminal status."),
                   eventTimestampMs);
    }
    else
    {
      // i18n-ignore: Stable task error fields from the AgentHost protocol.
      const QString errorMessageId = eventData.value(lit("errorMessageId")).toString();
      // i18n-ignore: Stable task error fields from the AgentHost protocol.
      const QJsonArray errorMessageArgs = eventData.value(lit("errorMessageArgs")).toArray();
      const QString reason = localizeTaskRuntimeMessage(errorMessageId, errorMessageArgs, summary);
      // i18n-ignore: Bounded technical diagnostic from the AgentHost protocol.
      const QString diagnostic = eventData.value(lit("error")).toString().left(4000);
      const QString rawDetail = diagnostic.isEmpty() ? summary : diagnostic;
      const QString detail = reason == summary ? rawDetail : reason + QLatin1String("\n\n") + rawDetail;
      finishTask(taskId, kind, detail, eventTimestampMs);
    }
  }
}

void AIAssistant::finishTask(const QString &taskId, const QString &kind, const QString &summary,
                             qint64 timestampMs)
{
  if(m_CompletedTasks.contains(taskId) || m_DeletedTasks.contains(taskId))
    return;
  m_CompletedTasks.insert(taskId);
  if(m_CompletedTasks.size() > 512)
  {
    m_CompletedTasks.clear();
    // finishChatProcess() polls TaskEvents before consulting this tombstone. Preserve the task
    // which caused compaction or its CLI JSON fallback would render the same answer a second time.
    m_CompletedTasks.insert(taskId);
  }

  QString accumulated = m_TaskTexts.take(taskId);
  // The normal live path accumulates directly in the transcript row to avoid a second shared
  // QString and a full-buffer detach on every provider delta. Copy it only once at the terminal.
  if(accumulated.isEmpty())
  {
    const AIChatMessage *taskItem = taskMessage(taskId);
    if(taskItem)
      accumulated = taskItem->provisionalText;
  }
  // Streaming deltas are provisional. A failed/cancelled terminal must never present an
  // incomplete model answer as the authoritative error; use only the terminal summary then.
  // i18n-ignore: Task lifecycle state identifiers used only for control flow.
  const bool answerTerminal = kind == lit("completed") || kind == lit("partial") ||
                              // i18n-ignore: Task lifecycle state identifier used only for control flow.
                              kind == lit("needs-user-input") ||
                              // i18n-ignore: Task lifecycle state identifier used only for control flow.
                              kind == lit("blocked");
  QString result = answerTerminal && !accumulated.isEmpty() ? accumulated : summary;
  // Prefer the model's complete streamed answer. The terminal summary is the fallback when
  // the provider did not stream prose.
  if(answerTerminal)
    result = result.trimmed();
  const QString conversationId = m_TaskConversationIds.value(taskId);
  const qint64 terminalTimestampMs =
      timestampMs > 0 ? timestampMs : QDateTime::currentMSecsSinceEpoch();
  // A provider heartbeat or streamed item can be the last event before the terminal turn event.
  // Close every remaining live row so a completed transcript never keeps saying "running".
  if(AIChatMessage *taskItem = taskMessage(taskId))
  {
    for(AIActivityItem &activity : taskItem->activity)
    {
      if(activity.completed)
        continue;
      activity.completed = true;
      activity.finishedTimestampMs = terminalTimestampMs;
      // i18n-ignore: Task lifecycle state identifier is used only for control flow.
      if(kind == lit("failed"))
        activity.error = true;
    }
  }
  if(answerTerminal)
  {
    QString title = tr("Turn ended");
    // i18n-ignore: Task lifecycle state identifier used only for label selection.
    if(kind == lit("needs-user-input"))
      title = tr("Waiting for input");
    // i18n-ignore: Task lifecycle state identifier used only for label selection.
    else if(kind == lit("partial"))
      title = tr("Turn ended with unfinished work");
    // i18n-ignore: Task lifecycle state identifier used only for label selection.
    else if(kind == lit("blocked"))
      title = tr("Blocked");
    // i18n-ignore: Activity kind is a backend protocol identifier; its visible title is translated.
    addTaskActivity(taskId, kind, title, QString(), true, false, terminalTimestampMs);
    setTaskState(taskId, kind, true);
    if(AIChatMessage *item = taskMessage(taskId))
      item->finishedTimestampMs = terminalTimestampMs;
    QString fallback = tr("This AI turn ended.");
    // i18n-ignore: Task lifecycle state identifier used only for label selection.
    if(kind == lit("needs-user-input"))
      fallback = tr("More information is required to continue.");
    // i18n-ignore: Task lifecycle state identifier used only for label selection.
    else if(kind == lit("partial"))
      fallback = tr("This AI turn ended with unfinished work.");
    // i18n-ignore: Task lifecycle state identifier used only for label selection.
    else if(kind == lit("blocked"))
      fallback = tr("The AI task is blocked.");
    appendAssistant(result.isEmpty() ? fallback : result, conversationId, taskId);
  }
  else
  {
    const QString state = kind == lit("cancelled") ? lit("cancelled") : lit("failed");
    AIChatMessage *item = taskMessage(taskId);
    if(item)
      item->provisionalText = result.isEmpty() ? tr("AI task failed without a message.") : result;
    addTaskActivity(taskId, state,
                    state == lit("cancelled") ? tr("Cancelled") : tr("Failed"), result, true,
                    // i18n-ignore: Task lifecycle state identifier is used only for error classification.
                    state != lit("cancelled"), terminalTimestampMs);
    setTaskState(taskId, state, true);
    if(item)
      item->finishedTimestampMs = terminalTimestampMs;
    if(ConversationThread *thread = conversation(conversationId))
      thread->unread = thread->id != m_ActiveConversationId;
  }
  m_UserPrompts.remove(taskId);
  unwatchTask(taskId);
  m_RunningTasks.remove(taskId);
  m_TaskOffsets.remove(taskId);
  m_TaskSequences.remove(taskId);
  m_TaskTails.remove(taskId);
  m_TaskAgentMessageStreams.remove(taskId);
  m_TaskConversationIds.remove(taskId);
  m_TaskMessageIds.remove(taskId);
  updateComposerState();
  renderConversation(true);
  // Terminal output is the authoritative conversation state. Capture an immutable generation so
  // an immediate capture close cannot leave only the earlier queued/running local snapshot.
  saveConversations();
  if(m_RunningTasks.isEmpty())
  {
    setDiagnosticStatus(m_Status, DiagnosticLevel::Success,
                        m_Ctx.IsCaptureLoaded() ? tr("Connected") : tr("Chat ready"));
  }
}

void AIAssistant::OnCaptureLoaded()
{
  // A fatal-replay rebuild reopens this exact file under a short-lived bridge
  // transaction. Its new controller/session must not cancel the task which is
  // recovering it. A different file always follows the normal cancellation path.
  // i18n-ignore: private, scoped recovery-transaction property name, not UI text.
  const QString recoveryPath =
      m_Ctx.GetMainWindow()->Widget()->property("breadReplayRecoveryCapturePath").toString();
  const rdcstr loadedFile = m_Ctx.GetCaptureFilename();
  const QString loadedPath = QString::fromUtf8(loadedFile.c_str(), int(loadedFile.size()));
  if(!recoveryPath.isEmpty() && !m_CaptureConversationPath.isEmpty() &&
     AIConversationStore::NormalizedCapturePath(recoveryPath) ==
         AIConversationStore::NormalizedCapturePath(m_CaptureConversationPath) &&
     AIConversationStore::NormalizedCapturePath(recoveryPath) ==
         AIConversationStore::NormalizedCapturePath(loadedPath))
  {
    updateCaptureStatus();
    return;
  }
  // A scratch/general-chat turn must never leak into a newly opened capture's conversation
  // namespace. Stop it, discard its queue, then hydrate this RDC's app-local history.
  clearCaptureTasks();
  const rdcstr captureFilename = m_Ctx.GetCaptureFilename();
  m_CaptureConversationPath =
      QString::fromUtf8(captureFilename.c_str(), int(captureFilename.size()));
  updateCaptureStatus();
  m_InstanceId.clear();
  loadConversations();
  refreshConnectionStatus();
}

void AIAssistant::OnCaptureClosed()
{
  // CloseCapture clears the native filename before notifying viewers, so the
  // retained conversation path is the identity available at this boundary.
  // OnCaptureLoaded additionally checks the actual reopened filename. The
  // bridge must clear this property in its transaction's finally block.
  // i18n-ignore: private, scoped recovery-transaction property name, not UI text.
  const QString recoveryPath =
      m_Ctx.GetMainWindow()->Widget()->property("breadReplayRecoveryCapturePath").toString();
  if(!recoveryPath.isEmpty() && !m_CaptureConversationPath.isEmpty() &&
     AIConversationStore::NormalizedCapturePath(recoveryPath) ==
         AIConversationStore::NormalizedCapturePath(m_CaptureConversationPath))
  {
    updateCaptureStatus();
    return;
  }
  if(m_ConversationSaveTimer)
    m_ConversationSaveTimer->stop();
  clearCaptureTasks();
  // clearCaptureTasks() has already written the final interrupted/running state under the cached
  // capture key. Do not serialize and write the identical document a second time.
  m_CaptureConversationPath.clear();
  m_InstanceId.clear();
  resetConversations();
  updateCaptureStatus();
}

void AIAssistant::OnSelectedEventChanged(uint32_t)
{
  updateCaptureStatus();
}

void AIAssistant::OnEventChanged(uint32_t)
{
  updateCaptureStatus();
}
