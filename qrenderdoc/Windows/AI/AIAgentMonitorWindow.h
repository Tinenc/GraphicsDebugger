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
#include <QFrame>
#include <QHash>
#include <QPointer>
#include <QString>

class AIAgentTraceStore;
class AIAgentTraceTabs;
class QCloseEvent;
class QComboBox;
class QLabel;
class QPushButton;
class QTimer;
class QTreeWidget;
class QTreeWidgetItem;

// Non-modal monitor for the complete normalized Agent trace. It can be added to qrenderdoc's
// ToolWindowManager or shown as a standalone top-level widget. Closing a standalone monitor hides
// the view; the externally-owned AIAgentTraceStore continues accumulating events.
class AIAgentMonitorWindow : public QFrame
{
public:
  explicit AIAgentMonitorWindow(AIAgentTraceStore &store, QWidget *parent = NULL);
  ~AIAgentMonitorWindow();

  QWidget *Widget() { return this; }

  void Open();
  void OpenEmpty();
  void HideAll();
  void ForgetTask(const QString &taskId);
  void SetTaskEventRoot(const QString &eventRoot);
  QString TaskEventRoot() const { return m_EventRoot; }
  bool OpenTask(const QString &taskId, QString *error = NULL);
  bool ReplayTaskFile(const QString &filePath, QString *error = NULL);
  QString CurrentTaskId() const;
  QString CurrentAgentId() const;

protected:
  void closeEvent(QCloseEvent *event) override;

private:
  void ScheduleRefresh();
  void RefreshFromStore();
  void RefreshTasks();
  void RefreshAgents();
  void SelectTask(int index);
  void SelectAgent(QTreeWidgetItem *item);
  void ChooseTaskFile();
  void OpenSelectedAgentWindow();
  QString TaskFilePath(const QString &taskId) const;
  void StopTailingTask();
  void BeginTailingTask(const QString &taskId, const QString &filePath, bool replayFromStart = false);
  void PollTaskFile();

  AIAgentTraceStore &m_Store;
  quint64 m_ObserverId = 0;
  QString m_EventRoot;
  QString m_RequestedTaskId;
  QString m_SelectedAgentId;
  QHash<QString, QString> m_TaskFiles;
  QString m_TailedTaskId;
  QString m_TailedFilePath;
  qint64 m_TailOffset = 0;
  qint64 m_TailBirthMs = 0;
  QByteArray m_TailBytes;
  bool m_DiscardingTailLine = false;
  bool m_ExplicitNoTask = false;
  qint64 m_LastStructureRefreshMs = 0;

  QComboBox *m_TaskCombo = NULL;
  QLabel *m_Status = NULL;
  QTreeWidget *m_AgentList = NULL;
  QPushButton *m_OpenAgent = NULL;
  AIAgentTraceTabs *m_TraceTabs = NULL;
  QTimer *m_RefreshTimer = NULL;
  QTimer *m_TaskTailTimer = NULL;
  QHash<QString, QPointer<QWidget>> m_AgentWindows;
};
