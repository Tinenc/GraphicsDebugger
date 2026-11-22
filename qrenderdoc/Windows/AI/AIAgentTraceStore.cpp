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

#include "AIAgentTraceStore.h"

#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QRegularExpression>
#include <QSet>
#include <QVariant>
#include <cmath>

namespace
{
// i18n-ignore: These are stable ReplayLab task-event protocol identifiers and field names.
const QString RawReasoningChannel = QStringLiteral("raw-reasoning");
const QString SummaryChannel = QStringLiteral("reasoning-summary");
const QString OpaqueChannel = QStringLiteral("opaque");
const QString MessageChannel = QStringLiteral("message");
const QString ToolStatusChannel = QStringLiteral("tools-status");
const QString CacheChannel = QStringLiteral("cache");
const qint64 MaxReplayTraceBytes = 32 * 1024 * 1024;
const qint64 MaxReplayEventLineBytes = 64 * 1024;
const int MaxAgentRecords = 2048;
const int AgentRecordTrimTarget = 1536;
const int MaxToolsStatusChars = 2 * 1024 * 1024;
const int MaxCacheMetadataChars = 1024 * 1024;
const int MaxTaskEventLogChars = 8 * 1024 * 1024;
const int TaskEventLogTrimTargetChars = 6 * 1024 * 1024;
const int MaxTaskEventLogDepth = 12;
const int MaxTaskEventLogValues = 4096;

struct AgentIdentity
{
  QString id;
  QString parentId;
  QString role;
  QString displayName;
  QString skill;
};

QString boundedString(const QJsonObject &object, const char *name, int maximum = 512)
{
  const QString value = object.value(QLatin1String(name)).toString().trimmed();
  return value.left(maximum);
}

QString firstString(const QStringList &values)
{
  for(const QString &value : values)
    if(!value.trimmed().isEmpty())
      return value.trimmed();
  return QString();
}

QString normalizedIdentifier(QString value)
{
  value = value.trimmed().toLower();
  value.replace(QLatin1Char('_'), QLatin1Char('-'));
  value.replace(QLatin1Char(' '), QLatin1Char('-'));
  return value;
}

bool containsIdentifier(const QString &value, const char *needle)
{
  return normalizedIdentifier(value).contains(QLatin1String(needle));
}

bool safeTaskId(const QString &taskId)
{
  // i18n-ignore: Validation expression for a filesystem-safe protocol identifier.
  static const QRegularExpression expression(QStringLiteral("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"));
  return expression.match(taskId).hasMatch();
}

bool positiveSequence(const QJsonValue &value, qint64 &sequence)
{
  if(!value.isDouble())
    return false;
  const double number = value.toDouble();
  // JSON numbers are represented as doubles by Qt 5. Require an exactly representable positive
  // integer so strings, fractions, infinities, and precision-colliding sequence values fail closed.
  const double MaximumExactJsonInteger = 9007199254740991.0;
  if(!std::isfinite(number) || number < 1.0 || number > MaximumExactJsonInteger ||
     std::floor(number) != number)
    return false;
  sequence = static_cast<qint64>(number);
  return true;
}

bool sensitiveKey(const QString &key)
{
  QString folded = key.toCaseFolded();
  folded.remove(QLatin1Char('-'));
  folded.remove(QLatin1Char('_'));
  folded.remove(QLatin1Char(' '));
  // i18n-ignore: Internal security-key fragments, never displayed as UI labels.
  static const char *const fragments[] = {"authorization", "cookie", "credential",  "password",
                                          "secret",        "apikey", "accesstoken", "refreshtoken"};
  for(const char *fragment : fragments)
    if(folded.contains(QLatin1String(fragment)))
      return true;
  // Provider-private payloads and signatures are never useful as plaintext UI diagnostics. Keep
  // adjacent metadata such as opaquePresent, opaqueBytes, and opaqueSha256 because those fields
  // prove that the provider returned non-readable reasoning without exposing it.
  // i18n-ignore: Internal security-key identifiers, never displayed as UI labels.
  return folded == QLatin1String("opaque") || folded == QLatin1String("ciphertext") ||
         folded.startsWith(QLatin1String("encryptedcontent")) ||
         folded.contains(QLatin1String("signature"));
}

QJsonValue redactedEventValue(const QString &key, const QJsonValue &value, int depth,
                              int &remainingValues)
{
  if(!key.isEmpty() && sensitiveKey(key))
    // i18n-ignore: Stable JSON diagnostic marker, not a user-interface label.
    return QStringLiteral("[redacted]");
  if(remainingValues <= 0)
    // i18n-ignore: Stable JSON diagnostic marker, not a user-interface label.
    return QStringLiteral("[value limit]");
  --remainingValues;
  if(depth >= MaxTaskEventLogDepth && (value.isObject() || value.isArray()))
    // i18n-ignore: Stable JSON diagnostic marker, not a user-interface label.
    return QStringLiteral("[depth limit]");
  if(value.isObject())
  {
    QJsonObject result;
    const QJsonObject object = value.toObject();
    for(QJsonObject::const_iterator it = object.constBegin(); it != object.constEnd(); ++it)
    {
      if(remainingValues <= 0)
      {
        // i18n-ignore: Stable JSON diagnostic field and marker, not user-interface labels.
        result.insert(QStringLiteral("_omitted"), QStringLiteral("[value limit]"));
        break;
      }
      result.insert(it.key(), redactedEventValue(it.key(), it.value(), depth + 1, remainingValues));
    }
    return result;
  }
  if(value.isArray())
  {
    QJsonArray result;
    const QJsonArray array = value.toArray();
    for(const QJsonValue &item : array)
    {
      if(remainingValues <= 0)
      {
        // i18n-ignore: Stable JSON diagnostic marker, not a user-interface label.
        result.push_back(QStringLiteral("[value limit]"));
        break;
      }
      result.push_back(redactedEventValue(QString(), item, depth + 1, remainingValues));
    }
    return result;
  }
  return value;
}

void appendTaskEventLog(AIAgentTraceTask &task, const QJsonObject &event)
{
  int remainingValues = MaxTaskEventLogValues;
  const QJsonObject redacted = redactedEventValue(QString(), event, 0, remainingValues).toObject();
  task.eventLog += QString::fromUtf8(QJsonDocument(redacted).toJson(QJsonDocument::Compact));
  task.eventLog += QLatin1Char('\n');
  if(task.eventLog.size() <= MaxTaskEventLogChars)
    return;

  const QString marker =
      QCoreApplication::translate("AIAgentTraceStore", "[Older task log events omitted]");
  // Leave substantial headroom after trimming. Trimming back to the exact limit made every later
  // token event copy almost the whole 8 MiB QString, saturating the GUI thread once a long task hit
  // the cap.
  const int excess = task.eventLog.size() - TaskEventLogTrimTargetChars + marker.size() + 1;
  int retainedFrom = task.eventLog.indexOf(QLatin1Char('\n'), qMax(0, excess));
  if(retainedFrom < 0)
    retainedFrom = task.eventLog.size();
  else
    ++retainedFrom;
  task.eventLog = marker + QLatin1Char('\n') + task.eventLog.mid(retainedFrom);
  ++task.eventLogRevision;
}

QString scalarText(const QJsonValue &value)
{
  if(value.isString())
    return value.toString();
  if(value.isBool())
    // i18n-ignore: JSON boolean serialization shown as diagnostic data.
    return value.toBool() ? QStringLiteral("true") : QStringLiteral("false");
  if(value.isDouble())
    return QString::number(value.toDouble(), 'g', 16);
  if(value.isNull())
    // i18n-ignore: JSON null serialization shown as diagnostic data.
    return QStringLiteral("null");
  if(value.isUndefined())
    return QString();
  return QString();
}

void appendValueLines(const QString &prefix, const QJsonValue &value, QStringList &lines,
                      int depth = 0)
{
  if(depth > 8 || prefix.isEmpty())
    return;
  if(sensitiveKey(prefix))
  {
    lines.push_back(prefix + QLatin1String(": ") +
                    QCoreApplication::translate("AIAgentTraceStore", "[redacted]"));
    return;
  }

  if(value.isObject())
  {
    const QJsonObject object = value.toObject();
    if(object.isEmpty())
    {
      // i18n-ignore: Empty JSON object serialization shown as diagnostic data.
      lines.push_back(prefix + QStringLiteral(": {}"));
      return;
    }
    for(QJsonObject::const_iterator it = object.constBegin(); it != object.constEnd(); ++it)
      appendValueLines(prefix + QLatin1Char('.') + it.key(), it.value(), lines, depth + 1);
    return;
  }

  if(value.isArray())
  {
    const QJsonArray array = value.toArray();
    if(array.isEmpty())
    {
      // i18n-ignore: Empty JSON array serialization shown as diagnostic data.
      lines.push_back(prefix + QStringLiteral(": []"));
      return;
    }
    bool scalar = true;
    QStringList values;
    for(const QJsonValue &entry : array)
    {
      if(entry.isObject() || entry.isArray())
      {
        scalar = false;
        break;
      }
      values.push_back(scalarText(entry));
    }
    if(scalar)
    {
      // i18n-ignore: Protocol field separator used in diagnostic data.
      lines.push_back(prefix + QLatin1String(": ") + values.join(QStringLiteral(", ")));
    }
    else
    {
      for(int i = 0; i < array.size(); ++i)
        appendValueLines(prefix + QLatin1Char('[') + QString::number(i) + QLatin1Char(']'),
                         array[i], lines, depth + 1);
    }
    return;
  }

  lines.push_back(prefix + QLatin1String(": ") + scalarText(value));
}

QString eventTime(qint64 timestampMs)
{
  if(timestampMs <= 0)
    return QString();
  return QDateTime::fromMSecsSinceEpoch(timestampMs).toString(QStringLiteral("HH:mm:ss.zzz"));
}

bool cacheKey(const QString &key)
{
  const QString folded = normalizedIdentifier(key);
  // i18n-ignore: Stable diagnostic field fragments used only to route metadata.
  static const char *const fragments[] = {"cache",        "context",    "token",      "usage",
                                          "model",        "profile",    "backend",    "thread",
                                          "turn",         "attempt",    "retry",      "elapsed",
                                          "duration",     "prompt",     "completion", "input-bytes",
                                          "output-bytes", "generation", "compaction", "stream-id"};
  for(const char *fragment : fragments)
    if(folded.contains(QLatin1String(fragment)))
      return true;
  return false;
}

QString cacheMetadataText(const QJsonObject &event, const QJsonObject &data, qint64 timestampMs)
{
  QStringList lines;
  for(QJsonObject::const_iterator it = event.constBegin(); it != event.constEnd(); ++it)
  {
    // i18n-ignore: Nested task-event payload is handled separately below.
    if(it.key() == QLatin1String("data") || !cacheKey(it.key()))
      continue;
    appendValueLines(it.key(), it.value(), lines);
  }
  for(QJsonObject::const_iterator it = data.constBegin(); it != data.constEnd(); ++it)
  {
    if(cacheKey(it.key()))
      appendValueLines(it.key(), it.value(), lines);
  }
  if(lines.isEmpty())
    return QString();

  const QString time = eventTime(timestampMs);
  const QString heading =
      time.isEmpty()
          ? QCoreApplication::translate("AIAgentTraceStore", "Cache and context metadata")
          : QCoreApplication::translate("AIAgentTraceStore", "[%1] Cache and context metadata").arg(time);
  lines.prepend(heading);
  return lines.join(QLatin1Char('\n'));
}

QString statusText(const QJsonObject &event, const QJsonObject &data, qint64 timestampMs,
                   bool includeAllMetadata)
{
  // i18n-ignore: Stable ReplayLab task-event protocol fields.
  const QString kind = boundedString(event, "kind", 64);
  const QString title = firstString(
      {boundedString(data, "activityTitle", 1000), boundedString(event, "summary", 4000), kind});
  const QString detail = boundedString(data, "activityDetail", 16000);
  const QString time = eventTime(timestampMs);
  QStringList lines;
  if(!title.isEmpty())
  {
    lines.push_back(
        time.isEmpty()
            ? title
            : QCoreApplication::translate("AIAgentTraceStore", "[%1] %2").arg(time, title));
  }
  if(!detail.isEmpty() && detail != title)
    lines.push_back(detail);

  // i18n-ignore: Stable diagnostic fields selected from the ReplayLab event payload.
  static const char *const statusFields[] = {"agentId",
                                             "parentAgentId",
                                             "agentRole",
                                             "planTaskId",
                                             "phase",
                                             "status",
                                             "skill",
                                             "source",
                                             "channel",
                                             "reasoningId",
                                             "lifecycle",
                                             "availability",
                                             "reset",
                                             "contentIndex",
                                             "summaryIndex",
                                             "contentFormat",
                                             "cacheFile",
                                             "opaquePresent",
                                             "opaqueBytes",
                                             "opaqueSha256",
                                             "activityId",
                                             "activityKind",
                                             "activityLifecycle",
                                             "tool",
                                             "durationMs",
                                             "exitCode",
                                             "attempt",
                                             "retry",
                                             "progressCurrent",
                                             "progressTotal"};
  QSet<QString> selected;
  for(const char *field : statusFields)
    selected.insert(QLatin1String(field));

  for(QJsonObject::const_iterator it = data.constBegin(); it != data.constEnd(); ++it)
  {
    // Titles and detail were rendered as prose above. Stream payload text belongs in its own tab.
    // i18n-ignore: Stable ReplayLab task-event protocol field names.
    if(it.key() == QLatin1String("activityTitle") || it.key() == QLatin1String("activityDetail") ||
       it.key() == QLatin1String("delta") || it.key() == QLatin1String("text") ||
       it.key() == QLatin1String("content"))
      continue;
    if(includeAllMetadata || selected.contains(it.key()))
      appendValueLines(it.key(), it.value(), lines);
  }

  // i18n-ignore: Stable ReplayLab task-event top-level tool field.
  if(event.contains(QLatin1String("tool")))
    appendValueLines(QStringLiteral("tool"), event.value(QLatin1String("tool")), lines);
  return lines.join(QLatin1Char('\n'));
}

AgentIdentity identityForEvent(const QJsonObject &event, const QJsonObject &data)
{
  Q_UNUSED(event);
  AgentIdentity identity;
  // i18n-ignore: Stable ReplayLab task-event protocol fields and role identifiers.
  const QString phase = boundedString(data, "phase", 128);
  const QString planTaskId = boundedString(data, "planTaskId", 256);
  identity.role = normalizedIdentifier(
      firstString({boundedString(data, "agentRole", 64), boundedString(data, "role", 64)}));
  // i18n-ignore: Provider chat streams and ReplayLab main events share one logical main Agent.
  if(identity.role == QLatin1String("chat"))
    identity.role = QStringLiteral("main");
  if(identity.role.isEmpty())
  {
    // i18n-ignore: Phase identifiers are stable ReplayLab task-event protocol values.
    if(!planTaskId.isEmpty() || containsIdentifier(phase, "child"))
      identity.role = QStringLiteral("child");
    else
      identity.role = QStringLiteral("main");
  }

  // A physical provider invocation id changes on retries and is still retained
  // in the event metadata. Route the view by logical Agent identity so that its
  // messages, tools, statuses, and all attempts remain in one detached window.
  if(!planTaskId.isEmpty())
    identity.id = planTaskId;
  else
    identity.id = firstString(
        {boundedString(data, "agentId", 256), boundedString(data, "agentInstanceId", 256)});
  if(identity.id.isEmpty())
    identity.id = identity.role;
  identity.parentId = boundedString(data, "parentAgentId", 256);
  if(identity.parentId.isEmpty() && identity.role == QLatin1String("child"))
    identity.parentId = QStringLiteral("main");
  if(identity.parentId == identity.id)
    identity.parentId.clear();
  identity.skill = boundedString(data, "skill", 256);
  identity.displayName =
      firstString({boundedString(data, "agentName", 256), boundedString(data, "displayName", 256),
                   identity.skill, boundedString(data, "childRole", 256), identity.id});
  return identity;
}

QString explicitChannel(const QJsonObject &data)
{
  // i18n-ignore: Stable ReplayLab task-event protocol field.
  return normalizedIdentifier(boundedString(data, "channel", 64));
}

QString primaryChannel(const QJsonObject &event, const QJsonObject &data)
{
  // i18n-ignore: Stable ReplayLab task-event protocol values used only for routing.
  const QString kind = normalizedIdentifier(boundedString(event, "kind", 64));
  const QString channel = explicitChannel(data);
  const QString activityKind = normalizedIdentifier(boundedString(data, "activityKind", 64));

  if(channel == QLatin1String("raw") || channel == QLatin1String("raw-reasoning") ||
     channel == QLatin1String("reasoning-raw") || channel == QLatin1String("chain-of-thought") ||
     channel == QLatin1String("thought") || channel == QLatin1String("reasoning"))
    return RawReasoningChannel;
  if(channel == QLatin1String("reasoning-summary") || channel == QLatin1String("summary"))
    return SummaryChannel;
  if(channel == QLatin1String("opaque"))
    return OpaqueChannel;
  if(channel == QLatin1String("message") || channel == QLatin1String("assistant-message") ||
     channel == QLatin1String("output"))
    return MessageChannel;

  if(kind == QLatin1String("message-delta") || kind == QLatin1String("agent-message-delta"))
    return MessageChannel;
  if(kind == QLatin1String("raw-reasoning") || kind == QLatin1String("reasoning-delta") ||
     kind == QLatin1String("agent-thought-delta"))
    return RawReasoningChannel;
  if(kind == QLatin1String("reasoning-summary") || activityKind == QLatin1String("reasoning"))
    return SummaryChannel;
  return QString();
}

QString streamText(const QJsonObject &event, const QJsonObject &data, const QString &channel)
{
  // i18n-ignore: Stable ReplayLab task-event protocol fields.
  if(event.value(QLatin1String("text")).isString())
    return event.value(QLatin1String("text")).toString();
  if(data.value(QLatin1String("delta")).isString())
    return data.value(QLatin1String("delta")).toString();
  if(data.value(QLatin1String("text")).isString())
    return data.value(QLatin1String("text")).toString();
  if(data.value(QLatin1String("content")).isString())
    return data.value(QLatin1String("content")).toString();
  if(channel == SummaryChannel && data.value(QLatin1String("activityDetail")).isString())
    return data.value(QLatin1String("activityDetail")).toString();
  if(event.value(QLatin1String("summary")).isString())
    return event.value(QLatin1String("summary")).toString();
  return QString();
}

bool toolEvent(const QJsonObject &event, const QJsonObject &data)
{
  // i18n-ignore: Stable ReplayLab task-event protocol values used only for routing.
  const QString kind = normalizedIdentifier(boundedString(event, "kind", 64));
  const QString activityKind = normalizedIdentifier(boundedString(data, "activityKind", 64));
  return kind.startsWith(QLatin1String("tool-")) || kind.contains(QLatin1String("tool-call")) ||
         activityKind.contains(QLatin1String("tool")) ||
         activityKind == QLatin1String("commandexecution") ||
         activityKind == QLatin1String("command-execution") ||
         activityKind == QLatin1String("websearch") || activityKind == QLatin1String("web-search");
}

QString activityId(const QJsonObject &data)
{
  // i18n-ignore: Stable ReplayLab task-event protocol fields.
  return firstString({boundedString(data, "reasoningId", 256), boundedString(data, "streamId", 256),
                      boundedString(data, "itemId", 256), boundedString(data, "activityId", 512)});
}

void appendSeparated(QString &target, const QString &text)
{
  if(text.isEmpty())
    return;
  if(!target.isEmpty() && !target.endsWith(QLatin1Char('\n')))
    target += QStringLiteral("\n\n");
  target += text;
}

void appendSeparatedBounded(QString &target, const QString &text, int maximumChars)
{
  if(text.isEmpty() || maximumChars <= 0)
    return;
  appendSeparated(target, text);
  if(target.size() <= maximumChars)
    return;

  const QString marker =
      QCoreApplication::translate("AIAgentTraceStore", "[Older trace entries omitted]");
  // Trim with hysteresis. Returning to the exact cap causes every subsequent append to copy the
  // whole buffer again, which is especially costly for long-running status streams.
  const int trimTarget = maximumChars * 3 / 4;
  const int retained = qMax(0, trimTarget - marker.size() - 2);
  target = marker + QStringLiteral("\n\n") + target.right(retained);
}

void appendBoundedRecord(AIAgentTraceAgent &agent, const AIAgentTraceRecord &record)
{
  agent.records.push_back(record);
  if(agent.records.size() > MaxAgentRecords)
  {
    // Leave headroom after compacting. Removing only the oldest record made every subsequent
    // provider delta shift the full 2048-element QVector until the stream ended.
    agent.records.remove(0, agent.records.size() - AgentRecordTrimTarget);
  }
}

QString physicalStreamId(const QJsonObject &data)
{
  // i18n-ignore: Stable ReplayLab task-event protocol fields.
  return firstString(
      {boundedString(data, "agentInstanceId", 256), boundedString(data, "agentId", 512)});
}

QString declaredStreamId(const QJsonObject &data)
{
  // i18n-ignore: Stable ReplayLab task-event protocol fields.
  return firstString({boundedString(data, "streamId", 256), boundedString(data, "stream", 64)});
}

void rebuildStreamText(const QMap<QString, QString> &snapshots, const QStringList &streamOrder,
                       const QString &prefix, QString &target)
{
  target.clear();
  for(const QString &key : streamOrder)
  {
    if(key.startsWith(prefix))
      appendSeparated(target, snapshots.value(key));
  }
}
}    // namespace

AIAgentTraceStore::AIAgentTraceStore() = default;

QString AIAgentTraceStore::ConversationReasoningKind(const QJsonObject &data)
{
  const QString channel = explicitChannel(data);
  if(channel == QLatin1String("summary") || channel == SummaryChannel)
    return QStringLiteral("reasoning");    // i18n-ignore: conversation activity kind.
  if(channel.isEmpty() || channel == QLatin1String("raw") || channel == RawReasoningChannel ||
     channel == QLatin1String("reasoning-raw") || channel == QLatin1String("reasoning") ||
     channel == QLatin1String("thought"))
    return QStringLiteral("reasoning-raw");    // i18n-ignore: conversation activity kind.
  return QString();
}

QString AIAgentTraceStore::ConversationSummaryPrefix(const QString &itemId)
{
  // i18n-ignore: conversation activity identity, never used as display text.
  return QStringLiteral("reasoning-summary:") + itemId.left(256) + QLatin1Char(':');
}

QString AIAgentTraceStore::ConversationReasoningId(const QJsonObject &data)
{
  const bool summary = ConversationReasoningKind(data) == QLatin1String("reasoning");
  QString itemId = firstString({boundedString(data, "itemId", 256),
                                boundedString(data, "reasoningId", 256),
                                boundedString(data, "streamId", 256)});
  if(itemId.isEmpty())
    itemId = QStringLiteral("live");    // i18n-ignore: fallback provider stream identity.
  const int index = data.value(summary ? QLatin1String("summaryIndex")
                                      : QLatin1String("contentIndex")).toInt();
  return (summary ? ConversationSummaryPrefix(itemId)
                  : QStringLiteral("reasoning-raw:") + itemId + QLatin1Char(':')) +
         QString::number(qMax(0, index));    // i18n-ignore: conversation activity identity.
}

AIAgentTraceStore::~AIAgentTraceStore() = default;

void AIAgentTraceStore::ConsumeEvent(const QJsonObject &event)
{
  ConsumeEventInternal(event, true);
}

bool AIAgentTraceStore::ConsumeEventInternal(const QJsonObject &event, bool notifyObservers,
                                             QString *changedTaskId, QString *changedAgentId)
{
  // i18n-ignore: Stable ReplayLab task-event protocol fields.
  if(event.value(QLatin1String("schema")).toString() != QLatin1String("replaylab-ai-task-event/v1"))
    return false;
  const QString taskId = event.value(QLatin1String("taskId")).toString().trimmed();
  if(!safeTaskId(taskId) || m_DeletedTasks.contains(taskId))
    return false;
  qint64 sequence = 0;
  if(!positiveSequence(event.value(QLatin1String("sequence")), sequence))
    return false;
  const qint64 timestampMs = event.value(QLatin1String("timestampUnixMs")).toVariant().toLongLong();
  const QJsonObject data = event.value(QLatin1String("data")).toObject();
  const AgentIdentity identity = identityForEvent(event, data);
  if(identity.id.isEmpty())
    return false;

  QMap<QString, AIAgentTraceTask>::iterator taskIt = m_Tasks.find(taskId);
  if(taskIt == m_Tasks.end())
  {
    AIAgentTraceTask task;
    task.id = taskId;
    taskIt = m_Tasks.insert(taskId, task);
    m_TaskOrder.push_back(taskId);
  }
  AIAgentTraceTask &task = taskIt.value();
  if(sequence > 0 && task.lastSequence > 0 && sequence <= task.lastSequence)
    return false;
  if(sequence > 0)
    task.lastSequence = sequence;
  appendTaskEventLog(task, event);
  if(task.firstTimestampMs <= 0 || (timestampMs > 0 && timestampMs < task.firstTimestampMs))
    task.firstTimestampMs = timestampMs;
  task.lastTimestampMs = qMax(task.lastTimestampMs, timestampMs);
  // i18n-ignore: Stable ReplayLab task-event protocol fields.
  const QString eventBackend = boundedString(event, "backend", 256);
  const QString eventProfile = boundedString(event, "profile", 256);
  const QString eventModel = boundedString(data, "model", 512);
  if(!eventBackend.isEmpty())
    task.backend = eventBackend;
  if(!eventProfile.isEmpty())
    task.profile = eventProfile;
  if(!eventModel.isEmpty())
    task.model = eventModel;

  QMap<QString, AIAgentTraceAgent>::iterator agentIt = task.agents.find(identity.id);
  if(agentIt == task.agents.end())
  {
    AIAgentTraceAgent agent;
    agent.id = identity.id;
    agentIt = task.agents.insert(identity.id, agent);
    task.agentOrder.push_back(identity.id);
  }
  AIAgentTraceAgent &agent = agentIt.value();
  if(!identity.parentId.isEmpty())
    agent.parentId = identity.parentId;
  if(!identity.role.isEmpty())
    agent.role = identity.role;
  if(!identity.displayName.isEmpty())
    agent.displayName = identity.displayName;
  if(!identity.skill.isEmpty())
    agent.skill = identity.skill;
  if(agent.firstTimestampMs <= 0 || (timestampMs > 0 && timestampMs < agent.firstTimestampMs))
    agent.firstTimestampMs = timestampMs;
  agent.lastTimestampMs = qMax(agent.lastTimestampMs, timestampMs);
  if(!eventBackend.isEmpty())
    agent.backend = eventBackend;
  if(!eventProfile.isEmpty())
    agent.profile = eventProfile;
  if(!eventModel.isEmpty())
    agent.model = eventModel;

  // i18n-ignore: Stable ReplayLab task-event protocol values used for lifecycle state.
  const QString kind = normalizedIdentifier(boundedString(event, "kind", 64));
  agent.lastEventKind = kind;
  const QString lifecycle = normalizedIdentifier(boundedString(data, "lifecycle", 64));
  const QString availability = normalizedIdentifier(boundedString(data, "availability", 64));
  if(!lifecycle.isEmpty())
    agent.lastLifecycle = lifecycle;
  if(!availability.isEmpty())
    agent.reasoningAvailability = availability;
  const QString explicitAgentStatus = boundedString(data, "agentStatus", 64);
  // i18n-ignore: Stable ReplayLab workflow terminal-status protocol field.
  const QString terminalStatus = normalizedIdentifier(boundedString(data, "terminalStatus", 64));
  if(!explicitAgentStatus.isEmpty())
    agent.status = explicitAgentStatus;
  else if(kind == QLatin1String("running") && identity.role == QLatin1String("child") &&
          boundedString(data, "phase", 64) == QLatin1String("child") &&
          !boundedString(data, "status", 64).isEmpty())
    agent.status = boundedString(data, "status", 64);
  else if(kind == QLatin1String("queued") || kind == QLatin1String("planning") ||
          kind == QLatin1String("completed") || kind == QLatin1String("partial") ||
          kind == QLatin1String("failed") || kind == QLatin1String("cancelled"))
    agent.status = kind == QLatin1String("completed") &&
                           (terminalStatus == QLatin1String("partial") ||
                            terminalStatus == QLatin1String("blocked") ||
                            terminalStatus == QLatin1String("needs-user-input"))
                       ? terminalStatus
                       : kind;
  else if(kind == QLatin1String("message-delta") || kind == QLatin1String("agent-message-delta") ||
          kind == QLatin1String("running"))
    agent.status = QStringLiteral("running");
  else if(agent.status.isEmpty())
    agent.status = QStringLiteral("running");

  // A running event's status belongs to its tool/activity, not the whole Agent. Likewise a
  // reasoning item may end or fail before the Agent recovers. Only an Agent status or terminal
  // turn event can end the Agent; tool failures remain visible in their individual records.
  if(kind == QLatin1String("reasoning-delta"))
  {
    if(lifecycle == QLatin1String("started") || lifecycle == QLatin1String("updated"))
      agent.status = QStringLiteral("running");
  }

  const QString channel = primaryChannel(event, data);
  if(channel == RawReasoningChannel)
  {
    if(!lifecycle.isEmpty() && !(agent.rawReasoningLifecycle == QLatin1String("truncated") &&
                                 lifecycle == QLatin1String("completed")))
      agent.rawReasoningLifecycle = lifecycle;
    if(!availability.isEmpty() &&
       !((agent.rawReasoningAvailability == QLatin1String("limit-reached") ||
          agent.rawReasoningAvailability == QLatin1String("state-limit")) &&
         availability == QLatin1String("available")))
      agent.rawReasoningAvailability = availability;
  }
  else if(channel == SummaryChannel)
  {
    if(!lifecycle.isEmpty())
      agent.summaryReasoningLifecycle = lifecycle;
    if(!availability.isEmpty())
      agent.summaryReasoningAvailability = availability;
  }
  else if(channel == OpaqueChannel)
  {
    // Signature-only/opaque provider reasoning has no readable raw stream. Reflect that state in
    // the Raw Reasoning tab instead of leaving the user with the ambiguous "nothing received"
    // placeholder. A later readable raw event is still allowed to upgrade this to available.
    if(agent.rawReasoningAvailability.isEmpty() ||
       agent.rawReasoningAvailability == QLatin1String("not-emitted"))
      agent.rawReasoningAvailability =
          availability.isEmpty() ? QStringLiteral("encrypted-only") : availability;
    if(agent.rawReasoning.isEmpty() && !lifecycle.isEmpty() &&
       agent.rawReasoningLifecycle != QLatin1String("truncated"))
      agent.rawReasoningLifecycle = lifecycle;
  }
  const QString sourceText = streamText(event, data, channel);
  const bool reset = data.value(QLatin1String("reset")).toBool();
  QString appendedText = sourceText;
  QString *target = NULL;
  quint64 *targetRevision = NULL;
  if(channel == RawReasoningChannel)
  {
    target = &agent.rawReasoning;
    targetRevision = &agent.rawReasoningRevision;
  }
  else if(channel == SummaryChannel)
  {
    target = &agent.reasoningSummary;
    targetRevision = &agent.reasoningSummaryRevision;
  }
  else if(channel == MessageChannel)
  {
    target = &agent.messages;
    targetRevision = &agent.messagesRevision;
  }

  if(target)
  {
    const QString invocationId = physicalStreamId(data);
    const QString declaredStream = declaredStreamId(data);
    QString itemStreamId = activityId(data);
    if(itemStreamId.isEmpty())
      // i18n-ignore: Internal fallback stream key, never displayed.
      itemStreamId = QStringLiteral("default");
    // Keep retry/provider invocations and declared answer streams distinct inside one logical
    // Agent. The unit separator is an internal composite-key delimiter, never displayed.
    const QString streamId = invocationId + QChar(0x1e) + declaredStream + QChar(0x1e) + itemStreamId;
    const QString snapshotPrefix =
        taskId + QChar(0x1f) + identity.id + QChar(0x1f) + channel + QChar(0x1f);
    const QString snapshotKey = snapshotPrefix + streamId;
    const bool newStream = !m_StreamSnapshots.contains(snapshotKey);
    if(newStream)
      m_StreamOrder.push_back(snapshotKey);
    const bool streamHadText = !m_StreamSnapshots.value(snapshotKey).isEmpty();

    // Timeline activity updates carry the complete public snapshot on each event. Convert those
    // updates into a per-stream snapshot. Direct provider deltas append exactly; reset replaces
    // only the named reasoning item, never unrelated items from the same Agent.
    // i18n-ignore: Stable ReplayLab task-event protocol fields.
    const bool explicitDelta = event.value(QLatin1String("text")).isString() ||
                               data.value(QLatin1String("delta")).isString();
    const bool snapshot = !streamId.isEmpty() && !explicitDelta &&
                          (data.contains(QLatin1String("lifecycle")) ||
                           data.contains(QLatin1String("activityLifecycle")) ||
                           data.value(QLatin1String("snapshot")).toBool());
    if(snapshot)
    {
      m_StreamSnapshots[snapshotKey] = sourceText;
      rebuildStreamText(m_StreamSnapshots, m_StreamOrder, snapshotPrefix, *target);
      if(targetRevision)
        ++*targetRevision;
    }
    else if(reset)
    {
      m_StreamSnapshots[snapshotKey] = sourceText;
      rebuildStreamText(m_StreamSnapshots, m_StreamOrder, snapshotPrefix, *target);
      if(targetRevision)
        ++*targetRevision;
    }
    else
    {
      m_StreamSnapshots[snapshotKey] += appendedText;
      // This is the hot token path. Append in O(1) amortized time; only authoritative snapshots
      // and reset events pay the cost of rebuilding all streams for the Agent/channel.
      if(!appendedText.isEmpty())
      {
        if(!streamHadText && !target->isEmpty() && !target->endsWith(QLatin1Char('\n')))
          *target += QStringLiteral("\n\n");
        *target += appendedText;
      }
    }
  }

  const bool isToolEvent = toolEvent(event, data);
  const bool isReasoningEvent = kind == QLatin1String("reasoning-delta");
  const bool isReasoningTokenEvent =
      isReasoningEvent && (lifecycle == QLatin1String("updated") || !sourceText.isEmpty());
  const bool isStreamOnly =
      kind == QLatin1String("message-delta") || kind == QLatin1String("agent-message-delta") ||
      kind == QLatin1String("reasoning-delta") || kind == QLatin1String("agent-thought-delta") ||
      kind == QLatin1String("raw-reasoning");
  // Token events already appear in the reasoning tabs. Repeating their routing/cache metadata for
  // every fragment made the status tabs grow far beyond the readable trace itself.
  if(isToolEvent || (isReasoningEvent && !isReasoningTokenEvent) || !isStreamOnly)
  {
    const QString status = statusText(event, data, timestampMs, isToolEvent);
    if(!status.isEmpty())
      appendSeparatedBounded(agent.toolsAndStatus, status, MaxToolsStatusChars);
  }

  // Backend/profile/cache fields are repeated on every provider token. They are envelope metadata,
  // not 25,000 distinct cache events, so retain them on lifecycle/tool records only.
  const QString cache = isStreamOnly ? QString() : cacheMetadataText(event, data, timestampMs);
  if(!cache.isEmpty())
    appendSeparatedBounded(agent.cacheMetadata, cache, MaxCacheMetadataChars);

  const QString recordActivityId =
      physicalStreamId(data) + QChar(0x1e) + declaredStreamId(data) + QChar(0x1e) + activityId(data);
  if(isReasoningTokenEvent)
  {
    // Retain one bounded diagnostic summary per contiguous reasoning stream instead of duplicating
    // every raw token and its complete JSON object. The lossless readable text lives in the stream
    // snapshot above and in the JSONL cache on disk.
    AIAgentTraceRecord *last = agent.records.isEmpty() ? NULL : &agent.records.last();
    if(last && last->kind == QLatin1String("reasoning-delta-summary") && last->channel == channel &&
       last->activityId == recordActivityId)
    {
      last->sequence = sequence;
      last->timestampMs = timestampMs;
      QJsonObject summaryData = last->event.value(QLatin1String("data")).toObject();
      summaryData[QStringLiteral("deltaEvents")] =
          summaryData.value(QLatin1String("deltaEvents")).toInt() + 1;
      summaryData[QStringLiteral("deltaChars")] =
          summaryData.value(QLatin1String("deltaChars")).toInt() + sourceText.size();
      summaryData[QStringLiteral("lifecycle")] = lifecycle;
      summaryData[QStringLiteral("availability")] = availability;
      if(reset)
        summaryData[QStringLiteral("resetEvents")] =
            summaryData.value(QLatin1String("resetEvents")).toInt() + 1;
      last->event[QStringLiteral("data")] = summaryData;
    }
    else
    {
      AIAgentTraceRecord record;
      record.sequence = sequence;
      record.timestampMs = timestampMs;
      // i18n-ignore: Internal trace-record kind, never displayed as user-facing prose.
      record.kind = QStringLiteral("reasoning-delta-summary");
      record.channel = channel;
      record.activityId = recordActivityId;
      QJsonObject summaryData;
      // i18n-ignore: Internal bounded trace-summary protocol fields.
      summaryData[QStringLiteral("deltaEvents")] = 1;
      summaryData[QStringLiteral("deltaChars")] = sourceText.size();
      summaryData[QStringLiteral("lifecycle")] = lifecycle;
      summaryData[QStringLiteral("availability")] = availability;
      summaryData[QStringLiteral("resetEvents")] = reset ? 1 : 0;
      QJsonObject summaryEvent;
      summaryEvent[QStringLiteral("schema")] = QStringLiteral("replaylab-ai-trace-summary/v1");
      summaryEvent[QStringLiteral("taskId")] = taskId;
      summaryEvent[QStringLiteral("kind")] = record.kind;
      summaryEvent[QStringLiteral("data")] = summaryData;
      record.event = summaryEvent;
      appendBoundedRecord(agent, record);
    }
  }
  else
  {
    AIAgentTraceRecord record;
    record.sequence = sequence;
    record.timestampMs = timestampMs;
    record.kind = kind;
    record.channel =
        channel.isEmpty() ? (isToolEvent ? ToolStatusChannel : QStringLiteral("status")) : channel;
    if(!cache.isEmpty() && channel.isEmpty() && !isToolEvent)
      record.channel = CacheChannel;
    record.activityId = recordActivityId;
    record.text = sourceText;
    record.event = event;
    appendBoundedRecord(agent, record);
  }

  if(changedTaskId)
    *changedTaskId = taskId;
  if(changedAgentId)
    *changedAgentId = identity.id;
  if(notifyObservers)
    Notify(taskId, identity.id);
  return true;
}

bool AIAgentTraceStore::ReplayTaskFile(const QString &filePath, QString *error,
                                       QString *replayedTaskId)
{
  return ReplayTaskFileInternal(filePath, QString(), error, replayedTaskId);
}

bool AIAgentTraceStore::ReplayTaskFileInternal(const QString &filePath, const QString &expectedTaskId,
                                               QString *error, QString *replayedTaskId)
{
  if(error)
    error->clear();
  if(replayedTaskId)
    replayedTaskId->clear();
  const QFileInfo fileInfo(filePath);
  if(fileInfo.isSymLink() || (fileInfo.exists() && !fileInfo.isFile()))
  {
    if(error)
      *error = QCoreApplication::translate("AIAgentTraceStore",
                                           "The task log path is not a regular file.");
    return false;
  }
  QFile file(filePath);
  if(!file.open(QIODevice::ReadOnly | QIODevice::Text))
  {
    if(error)
      *error = QCoreApplication::translate("AIAgentTraceStore", "Could not open task log: %1")
                   .arg(file.errorString());
    return false;
  }
  if(file.size() > MaxReplayTraceBytes)
  {
    if(error)
      *error = QCoreApplication::translate("AIAgentTraceStore",
                                           "The task log exceeds the local display limit.");
    return false;
  }

  auto readEvent = [&file](QJsonObject &object, bool &hasObject, bool &skipped) {
    object = QJsonObject();
    hasObject = false;
    skipped = false;
    if(file.atEnd())
      return false;
    QByteArray line = file.readLine(MaxReplayEventLineBytes + 2);
    if(line.size() > MaxReplayEventLineBytes || (!line.endsWith('\n') && !file.atEnd()))
    {
      while(!file.atEnd() && !line.endsWith('\n'))
        line = file.readLine(MaxReplayEventLineBytes + 2);
      skipped = true;
      return true;
    }
    line = line.trimmed();
    if(line.isEmpty())
      return true;
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(line, &parseError);
    if(parseError.error != QJsonParseError::NoError || !document.isObject())
    {
      skipped = true;
      return true;
    }
    object = document.object();
    hasObject = true;
    return true;
  };
  auto validEnvelope = [](const QJsonObject &object, QString &taskId) {
    taskId = object.value(QLatin1String("taskId")).toString().trimmed();
    qint64 sequence = 0;
    return object.value(QLatin1String("schema")).toString() ==
               QLatin1String("replaylab-ai-task-event/v1") &&
           safeTaskId(taskId) && positiveSequence(object.value(QLatin1String("sequence")), sequence);
  };

  // Validate the complete file before mutating the shared store. A manually selected JSONL file
  // must describe exactly one task, and ReplayTaskId must not accept a renamed/misdirected log.
  QString logTaskId;
  bool validAny = false;
  int skippedLines = 0;
  while(true)
  {
    QJsonObject object;
    bool hasObject = false;
    bool skipped = false;
    if(!readEvent(object, hasObject, skipped))
      break;
    if(skipped)
      ++skippedLines;
    if(!hasObject)
      continue;
    QString envelopeTaskId;
    if(!validEnvelope(object, envelopeTaskId))
    {
      ++skippedLines;
      continue;
    }
    if(!expectedTaskId.isEmpty() && envelopeTaskId != expectedTaskId)
    {
      if(error)
        *error = QCoreApplication::translate("AIAgentTraceStore",
                                             "The task log does not belong to task %1.")
                     .arg(expectedTaskId);
      return false;
    }
    if(!logTaskId.isEmpty() && envelopeTaskId != logTaskId)
    {
      if(error)
        *error = QCoreApplication::translate(
            "AIAgentTraceStore", "The task log contains events for more than one task.");
      return false;
    }
    logTaskId = envelopeTaskId;
    validAny = true;
  }

  if(!validAny)
  {
    if(error)
      *error = QCoreApplication::translate("AIAgentTraceStore",
                                           "The task log did not contain any valid events.");
    return false;
  }
  if(replayedTaskId)
    *replayedTaskId = logTaskId;
  if(!file.seek(0))
  {
    if(error)
      *error = QCoreApplication::translate("AIAgentTraceStore", "Could not read task log: %1")
                   .arg(file.errorString());
    return false;
  }

  QSet<QString> changed;
  while(true)
  {
    QJsonObject object;
    bool hasObject = false;
    bool skipped = false;
    if(!readEvent(object, hasObject, skipped))
      break;
    if(!hasObject)
      continue;
    QString envelopeTaskId;
    if(!validEnvelope(object, envelopeTaskId) || envelopeTaskId != logTaskId)
      continue;
    QString taskId;
    QString agentId;
    if(ConsumeEventInternal(object, false, &taskId, &agentId))
      changed.insert(taskId + QChar(0x1f) + agentId);
  }

  for(const QString &key : changed)
  {
    const int separator = key.indexOf(QChar(0x1f));
    Notify(key.left(separator), key.mid(separator + 1));
  }
  if(error && skippedLines > 0)
    *error = QCoreApplication::translate("AIAgentTraceStore",
                                         "Loaded the task log with %1 skipped event line(s).")
                 .arg(skippedLines);
  return true;
}

bool AIAgentTraceStore::ReplayTaskId(const QString &taskId, const QString &eventRoot, QString *error)
{
  if(!safeTaskId(taskId) || m_DeletedTasks.contains(taskId) ||
     QFileInfo::exists(QDir(eventRoot).filePath(QStringLiteral(".deleted-tasks/") + taskId +
                                              QStringLiteral(".json"))))
  {
    if(error)
      *error =
          QCoreApplication::translate("AIAgentTraceStore", "The task identifier is not valid.");
    return false;
  }
  const QDir root(eventRoot);
  const QString filePath = root.filePath(taskId + QStringLiteral(".jsonl"));
  const QFileInfo fileInfo(filePath);
  if(fileInfo.exists())
  {
    const QString canonicalRoot = QFileInfo(root.absolutePath()).canonicalFilePath();
    const QString canonicalFile = fileInfo.canonicalFilePath();
    const QString expectedFile = QDir(canonicalRoot).filePath(fileInfo.fileName());
#if defined(Q_OS_WIN)
    const Qt::CaseSensitivity pathCase = Qt::CaseInsensitive;
#else
    const Qt::CaseSensitivity pathCase = Qt::CaseSensitive;
#endif
    if(fileInfo.isSymLink() || !fileInfo.isFile() || canonicalRoot.isEmpty() ||
       canonicalFile.isEmpty() ||
       QDir::cleanPath(canonicalFile).compare(QDir::cleanPath(expectedFile), pathCase) != 0)
    {
      if(error)
        *error = QCoreApplication::translate("AIAgentTraceStore",
                                             "The task log path is not a regular file.");
      return false;
    }
  }
  return ReplayTaskFileInternal(filePath, taskId, error, NULL);
}

QStringList AIAgentTraceStore::DiscoverTaskIds(const QString &eventRoot, int maximumCount) const
{
  QStringList result;
  if(maximumCount <= 0)
    return result;
  QDir directory(eventRoot);
  if(!directory.exists())
    return result;
  // i18n-ignore: Task-event filename extension and wildcard are filesystem protocol values.
  const QFileInfoList files = directory.entryInfoList(QStringList() << QStringLiteral("*.jsonl"),
                                                      QDir::Files | QDir::NoSymLinks, QDir::Time);
  for(const QFileInfo &file : files)
  {
    if(result.size() >= maximumCount)
      break;
    const QString taskId = file.completeBaseName();
    if(safeTaskId(taskId) && !m_DeletedTasks.contains(taskId) &&
       !QFileInfo::exists(directory.filePath(QStringLiteral(".deleted-tasks/") + taskId +
                                             QStringLiteral(".json"))))
      result.push_back(taskId);
  }
  return result;
}

bool AIAgentTraceStore::ArchiveTaskLog(const QString &taskId, const QString &eventRoot,
                                       const QString &reason) const
{
  if(!safeTaskId(taskId) || eventRoot.isEmpty())
    return false;
  QDir directory(eventRoot);
  if(!directory.exists())
    return false;
  const QString jsonlPath = directory.filePath(taskId + QStringLiteral(".jsonl"));
  if(!QFileInfo::exists(jsonlPath) || QFileInfo(jsonlPath).isSymLink())
    return false;
  const QString archivePath = directory.filePath(taskId + QStringLiteral(".archive.json"));
  QJsonObject payload;
  payload.insert(QStringLiteral("schema"), QStringLiteral("replaylab-task-archive/v1"));
  payload.insert(QStringLiteral("schemaVersion"), 1);
  payload.insert(QStringLiteral("taskId"), taskId);
  payload.insert(QStringLiteral("archivedAtUnixMs"), QDateTime::currentMSecsSinceEpoch());
  payload.insert(QStringLiteral("reason"), reason.left(128));
  payload.insert(QStringLiteral("retentionDays"), 7);
  QFile file(archivePath);
  if(!file.open(QIODevice::WriteOnly | QIODevice::Truncate))
    return false;
  file.write(QJsonDocument(payload).toJson(QJsonDocument::Compact));
  file.write("\n");
  return true;
}

bool AIAgentTraceStore::IsTaskLogArchived(const QString &taskId, const QString &eventRoot) const
{
  if(!safeTaskId(taskId) || eventRoot.isEmpty())
    return false;
  const QString archivePath =
      QDir(eventRoot).filePath(taskId + QStringLiteral(".archive.json"));
  return QFileInfo::exists(archivePath) && !QFileInfo(archivePath).isSymLink();
}

int AIAgentTraceStore::PurgeExpiredArchivedTaskLogs(const QString &eventRoot,
                                                    int retentionDays) const
{
  if(eventRoot.isEmpty() || retentionDays <= 0)
    return 0;
  QDir directory(eventRoot);
  if(!directory.exists())
    return 0;
  const qint64 cutoffMs =
      QDateTime::currentMSecsSinceEpoch() - qint64(retentionDays) * 24 * 60 * 60 * 1000;
  int removed = 0;
  // i18n-ignore: archive sidecar extension is a filesystem protocol value.
  const QFileInfoList archives = directory.entryInfoList(
      QStringList() << QStringLiteral("*.archive.json"), QDir::Files | QDir::NoSymLinks);
  for(const QFileInfo &archiveInfo : archives)
  {
    const QString taskId = archiveInfo.completeBaseName();
    if(!safeTaskId(taskId))
      continue;
    qint64 archivedAt = archiveInfo.lastModified().toMSecsSinceEpoch();
    QFile archiveFile(archiveInfo.absoluteFilePath());
    if(archiveFile.open(QIODevice::ReadOnly))
    {
      const QJsonDocument document = QJsonDocument::fromJson(archiveFile.readAll());
      archiveFile.close();
      if(document.isObject())
      {
        const qint64 stamped = document.object().value(QStringLiteral("archivedAtUnixMs")).toVariant().toLongLong();
        if(stamped > 0)
          archivedAt = stamped;
      }
    }
    if(archivedAt > cutoffMs)
      continue;
    const QStringList suffixes = QStringList()
                                 << QStringLiteral(".jsonl") << QStringLiteral(".jsonl.lock")
                                 << QStringLiteral(".report.json")
                                 << QStringLiteral(".report-state.json")
                                 << QStringLiteral(".report-state.lock")
                                 << QStringLiteral(".archive.json");
    for(const QString &suffix : suffixes)
    {
      const QString path = directory.filePath(taskId + suffix);
      if(QFileInfo::exists(path) && !QFileInfo(path).isSymLink() && QFile::remove(path))
        ++removed;
    }
  }
  return removed;
}

QStringList AIAgentTraceStore::TaskIds() const
{
  return m_TaskOrder;
}

QStringList AIAgentTraceStore::AgentIds(const QString &taskId) const
{
  const QMap<QString, AIAgentTraceTask>::const_iterator it = m_Tasks.constFind(taskId);
  return it == m_Tasks.constEnd() ? QStringList() : it.value().agentOrder;
}

const AIAgentTraceTask *AIAgentTraceStore::Task(const QString &taskId) const
{
  const QMap<QString, AIAgentTraceTask>::const_iterator it = m_Tasks.constFind(taskId);
  return it == m_Tasks.constEnd() ? NULL : &it.value();
}

const AIAgentTraceAgent *AIAgentTraceStore::Agent(const QString &taskId, const QString &agentId) const
{
  const AIAgentTraceTask *task = Task(taskId);
  if(!task)
    return NULL;
  const QMap<QString, AIAgentTraceAgent>::const_iterator it = task->agents.constFind(agentId);
  return it == task->agents.constEnd() ? NULL : &it.value();
}

quint64 AIAgentTraceStore::AddObserver(ChangeObserver observer)
{
  if(!observer)
    return 0;
  const quint64 id = m_NextObserverId++;
  m_Observers.insert(id, observer);
  return id;
}

void AIAgentTraceStore::RemoveObserver(quint64 observerId)
{
  m_Observers.remove(observerId);
}

void AIAgentTraceStore::Clear()
{
  m_Tasks.clear();
  m_TaskOrder.clear();
  m_StreamSnapshots.clear();
  m_StreamOrder.clear();
  Notify(QString(), QString());
}

void AIAgentTraceStore::ForgetTask(const QString &taskId)
{
  if(!safeTaskId(taskId))
    return;
  m_DeletedTasks.insert(taskId);
  m_Tasks.remove(taskId);
  m_TaskOrder.removeAll(taskId);
  const QString prefix = taskId + QChar(0x1f);
  for(int i = m_StreamOrder.size() - 1; i >= 0; --i)
  {
    if(m_StreamOrder[i].startsWith(prefix))
    {
      m_StreamSnapshots.remove(m_StreamOrder[i]);
      m_StreamOrder.removeAt(i);
    }
  }
  Notify(taskId, QString());
}

void AIAgentTraceStore::Notify(const QString &taskId, const QString &agentId)
{
  // Copy the callbacks so a view may safely unregister itself while handling a notification.
  const QList<ChangeObserver> observers = m_Observers.values();
  for(const ChangeObserver &observer : observers)
    if(observer)
      observer(taskId, agentId);
}
