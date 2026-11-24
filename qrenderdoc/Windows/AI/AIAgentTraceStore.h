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

#include <QJsonObject>
#include <QMap>
#include <QSet>
#include <QString>
#include <QStringList>
#include <QVector>
#include <functional>

// One normalized task-event record. Non-stream events retain the original object; high-frequency
// reasoning deltas are represented by bounded aggregate records because their lossless text lives
// in the dedicated streams and on-disk JSONL cache.
struct AIAgentTraceRecord
{
  qint64 sequence = 0;
  qint64 timestampMs = 0;
  QString kind;
  QString channel;
  QString activityId;
  QString text;
  QJsonObject event;
};

// Accumulated plain-text streams and metadata for one main Agent or deepagents sub-agent.
// Stream text is deliberately kept separate from the ordinary Assistant conversation store: raw
// trace data must never be fed back to a later model turn as chat history.
struct AIAgentTraceAgent
{
  QString id;
  QString parentId;
  QString role;
  QString displayName;
  QString skill;
  QString status;
  QString backend;
  QString profile;
  QString model;
  QString lastEventKind;
  QString lastLifecycle;
  QString reasoningAvailability;
  QString rawReasoningLifecycle;
  QString rawReasoningAvailability;
  QString summaryReasoningLifecycle;
  QString summaryReasoningAvailability;
  qint64 firstTimestampMs = 0;
  qint64 lastTimestampMs = 0;

  QString rawReasoning;
  QString reasoningSummary;
  QString messages;
  quint64 rawReasoningRevision = 0;
  quint64 reasoningSummaryRevision = 0;
  quint64 messagesRevision = 0;
  QString toolsAndStatus;
  QString cacheMetadata;
  QVector<AIAgentTraceRecord> records;
};

struct AIAgentTraceTask
{
  QString id;
  QString backend;
  QString profile;
  QString model;
  qint64 firstTimestampMs = 0;
  qint64 lastTimestampMs = 0;
  qint64 lastSequence = 0;
  // Bounded, redacted JSONL reconstructed from every accepted task event. This is the task-wide
  // debugging surface; per-Agent message and reasoning streams below remain easier to read.
  QString eventLog;
  quint64 eventLogRevision = 0;
  QMap<QString, AIAgentTraceAgent> agents;
  QStringList agentOrder;
};

// GUI-thread store shared by the Assistant event consumer and any number of monitor windows. Views
// may be destroyed or hidden without interrupting accumulation. Completed task JSONL files can be
// replayed lazily into the same normalized model.
class AIAgentTraceStore
{
public:
  typedef std::function<void(const QString &taskId, const QString &agentId)> ChangeObserver;

  AIAgentTraceStore();
  ~AIAgentTraceStore();

  void ConsumeEvent(const QJsonObject &event);

  // Route only readable provider-emitted channels. Opaque/encrypted events stay in diagnostics.
  static QString ConversationReasoningKind(const QJsonObject &data);
  static QString ConversationReasoningId(const QJsonObject &data);
  static QString ConversationSummaryPrefix(const QString &itemId);

  bool ReplayTaskFile(const QString &filePath, QString *error = NULL, QString *replayedTaskId = NULL);
  bool ReplayTaskId(const QString &taskId, const QString &eventRoot, QString *error = NULL);
  QStringList DiscoverTaskIds(const QString &eventRoot, int maximumCount = 128) const;
  // Mark a finished task log as archived (kept for restore for retentionDays).
  bool ArchiveTaskLog(const QString &taskId, const QString &eventRoot,
                      const QString &reason = QString()) const;
  bool IsTaskLogArchived(const QString &taskId, const QString &eventRoot) const;
  // Delete archived task sidecars older than retentionDays, and remove orphaned
  // lock/report files for those tasks. Active (non-archived) logs are kept.
  int PurgeExpiredArchivedTaskLogs(const QString &eventRoot, int retentionDays = 7) const;

  QStringList TaskIds() const;
  QStringList AgentIds(const QString &taskId) const;
  const AIAgentTraceTask *Task(const QString &taskId) const;
  const AIAgentTraceAgent *Agent(const QString &taskId, const QString &agentId) const;

  quint64 AddObserver(ChangeObserver observer);
  void RemoveObserver(quint64 observerId);
  // Forget one removed conversation's task, including any delayed event delivery.
  void ForgetTask(const QString &taskId);
  void Clear();

private:
  bool ReplayTaskFileInternal(const QString &filePath, const QString &expectedTaskId,
                              QString *error, QString *replayedTaskId);
  bool ConsumeEventInternal(const QJsonObject &event, bool notifyObservers,
                            QString *changedTaskId = NULL, QString *changedAgentId = NULL);
  void Notify(const QString &taskId, const QString &agentId);

  QMap<QString, AIAgentTraceTask> m_Tasks;
  QStringList m_TaskOrder;
  QSet<QString> m_DeletedTasks;
  QMap<QString, QString> m_StreamSnapshots;
  QStringList m_StreamOrder;
  QMap<quint64, ChangeObserver> m_Observers;
  quint64 m_NextObserverId = 1;
};
