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

#include "AIAgentMonitorWindow.h"

#include <QCloseEvent>
#include <QComboBox>
#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QFontDatabase>
#include <QHBoxLayout>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QLabel>
#include <QLocale>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QRegularExpression>
#include <QScrollBar>
#include <QSet>
#include <QSignalBlocker>
#include <QSplitter>
#include <QTabWidget>
#include <QTextCursor>
#include <QTextDocument>
#include <QTimer>
#include <QTreeWidget>
#include <QTreeWidgetItem>
#include <QVBoxLayout>
#include <QVariant>

#include "AIAgentTraceStore.h"

namespace
{
const qint64 MaxInitialTailHistoryBytes = 32 * 1024 * 1024;
const qint64 MaxSynchronousReplayBytes = 1024 * 1024;
const qint64 MaxTailReadBytes = 128 * 1024;
const int MaxTailEventLineBytes = 64 * 1024;
const int MaxMaterializedTaskLogChars = 1024 * 1024;
const int TaskLogTrimTargetChars = 768 * 1024;

bool safeTaskId(const QString &taskId)
{
  // i18n-ignore: Validation expression for a filesystem-safe protocol identifier.
  static const QRegularExpression expression(QStringLiteral("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"));
  return expression.match(taskId).hasMatch();
}

QString roleText(const AIAgentTraceAgent &agent)
{
  // i18n-ignore: Agent roles are stable ReplayLab protocol identifiers used only for selection.
  const QString role = agent.role.trimmed().toLower();
  if(role == QLatin1String("main"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Main Agent");
  if(role == QLatin1String("child"))
  {
    const QString name = !agent.skill.isEmpty() ? agent.skill : agent.displayName;
    return name.isEmpty()
               ? QCoreApplication::translate("AIAgentMonitorWindow", "Child Agent")
               : QCoreApplication::translate("AIAgentMonitorWindow", "Child Agent: %1").arg(name);
  }
  const QString name = !agent.displayName.isEmpty() ? agent.displayName : agent.id;
  return name.isEmpty() ? QCoreApplication::translate("AIAgentMonitorWindow", "Agent")
                        : QCoreApplication::translate("AIAgentMonitorWindow", "Agent: %1").arg(name);
}

QString statusText(const QString &status)
{
  // i18n-ignore: Lifecycle states are stable ReplayLab protocol identifiers used for lookup.
  const QString normalized = status.trimmed().toLower();
  if(normalized == QLatin1String("queued"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Queued");
  if(normalized == QLatin1String("planning"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Planning");
  if(normalized == QLatin1String("running") || normalized == QLatin1String("waiting"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Running");
  if(normalized == QLatin1String("completed"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Completed");
  if(normalized == QLatin1String("partial"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Completed with limitations");
  if(normalized == QLatin1String("failed"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Failed");
  if(normalized == QLatin1String("cancelled"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Cancelled");
  if(normalized == QLatin1String("blocked"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Blocked");
  if(normalized == QLatin1String("needs-user-input"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Waiting for input");
  return status;
}

QString lifecycleText(const QString &lifecycle)
{
  // i18n-ignore: Reasoning lifecycle values are stable ReplayLab protocol identifiers.
  const QString normalized = lifecycle.trimmed().toLower();
  if(normalized == QLatin1String("started"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Started");
  if(normalized == QLatin1String("updated"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Updated");
  if(normalized == QLatin1String("completed"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Completed");
  if(normalized == QLatin1String("unavailable"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Unavailable");
  if(normalized == QLatin1String("truncated"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Truncated");
  if(normalized == QLatin1String("failed"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Failed");
  return lifecycle;
}

QString availabilityText(const QString &availability)
{
  // i18n-ignore: Reasoning availability values are stable ReplayLab protocol identifiers.
  const QString normalized = availability.trimmed().toLower();
  if(normalized == QLatin1String("available"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Available");
  if(normalized == QLatin1String("not-emitted"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Not emitted");
  if(normalized == QLatin1String("encrypted-only"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Encrypted only");
  if(normalized == QLatin1String("limit-reached"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Limit reached");
  if(normalized == QLatin1String("state-limit"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "State limit reached");
  if(normalized == QLatin1String("unsupported"))
    return QCoreApplication::translate("AIAgentMonitorWindow", "Unsupported");
  return availability;
}

QString rawReasoningPlaceholder(const AIAgentTraceAgent *agent)
{
  if(agent)
  {
    // i18n-ignore: Reasoning state values are stable ReplayLab protocol identifiers.
    const QString availability = agent->rawReasoningAvailability.trimmed().toLower();
    if(!availability.isEmpty() && availability != QLatin1String("available"))
      return QCoreApplication::translate("AIAgentMonitorWindow", "Raw reasoning is unavailable: %1")
          .arg(availabilityText(agent->rawReasoningAvailability));

    const QString lifecycle = agent->rawReasoningLifecycle.trimmed().toLower();
    if(lifecycle == QLatin1String("unavailable") || lifecycle == QLatin1String("truncated") ||
       lifecycle == QLatin1String("failed"))
      return QCoreApplication::translate("AIAgentMonitorWindow",
                                         "Raw reasoning ended with state: %1")
          .arg(lifecycleText(agent->rawReasoningLifecycle));
  }

  return QCoreApplication::translate("AIAgentMonitorWindow",
                                     "No raw reasoning has been received for this Agent.");
}

void configureTextView(QPlainTextEdit *edit, const QString &placeholder, bool wrap)
{
  edit->setReadOnly(true);
  edit->setUndoRedoEnabled(false);
  edit->setFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));
  edit->setLineWrapMode(wrap ? QPlainTextEdit::WidgetWidth : QPlainTextEdit::NoWrap);
  edit->setPlaceholderText(placeholder);
}

void updateTextView(QPlainTextEdit *edit, QString &rendered, const QString &text)
{
  QScrollBar *scroll = edit->verticalScrollBar();
  const bool followTail = !scroll || scroll->maximum() - scroll->value() <= 2;
  if(text.startsWith(rendered))
  {
    const QString delta = text.mid(rendered.size());
    if(!delta.isEmpty())
    {
      QTextCursor cursor = edit->textCursor();
      cursor.movePosition(QTextCursor::End);
      cursor.insertText(delta);
    }
  }
  else
  {
    edit->setPlainText(text);
  }
  rendered = text;
  if(followTail)
  {
    QTextCursor cursor = edit->textCursor();
    cursor.movePosition(QTextCursor::End);
    edit->setTextCursor(cursor);
  }
}

void updateStreamingTextView(QPlainTextEdit *edit, int &renderedChars, quint64 &renderedRevision,
                             const QString &text, quint64 revision)
{
  QScrollBar *scroll = edit->verticalScrollBar();
  const bool followTail = !scroll || scroll->maximum() - scroll->value() <= 2;
  if(renderedRevision == revision && renderedChars <= text.size())
  {
    const QString delta = text.mid(renderedChars);
    if(!delta.isEmpty())
    {
      QTextCursor cursor = edit->textCursor();
      cursor.movePosition(QTextCursor::End);
      cursor.insertText(delta);
    }
  }
  else
  {
    edit->setPlainText(text);
  }
  renderedChars = text.size();
  renderedRevision = revision;
  if(followTail)
  {
    QTextCursor cursor = edit->textCursor();
    cursor.movePosition(QTextCursor::End);
    edit->setTextCursor(cursor);
  }
}

QString materializedTaskLog(const QString &text, int maximumChars)
{
  if(text.size() <= maximumChars)
    return text;

  const QString marker =
      QCoreApplication::translate("AIAgentTraceStore", "[Older task log events omitted]");
  const int retained = qMax(0, maximumChars - marker.size() - 1);
  int retainedFrom = qMax(0, text.size() - retained);
  const int newline = text.indexOf(QLatin1Char('\n'), retainedFrom);
  if(newline >= 0)
    retainedFrom = newline + 1;
  return marker + QLatin1Char('\n') + text.mid(retainedFrom);
}

void updateTaskLogView(QPlainTextEdit *edit, int &renderedSourceChars,
                       quint64 &renderedRevision, const QString &text, quint64 revision)
{
  QScrollBar *scroll = edit->verticalScrollBar();
  const bool followTail = !scroll || scroll->maximum() - scroll->value() <= 2;
  const int oldScrollValue = scroll ? scroll->value() : 0;
  bool replacedDocument = false;

  if(renderedRevision != revision || renderedSourceChars > text.size() ||
     renderedSourceChars == 0)
  {
    const int initialLimit = text.size() > MaxMaterializedTaskLogChars
                                 ? TaskLogTrimTargetChars
                                 : MaxMaterializedTaskLogChars;
    edit->setPlainText(materializedTaskLog(text, initialLimit));
    replacedDocument = true;
  }
  else if(renderedSourceChars < text.size())
  {
    const int materializedChars = qMax(0, edit->document()->characterCount() - 1);
    const int deltaChars = text.size() - renderedSourceChars;
    if(deltaChars > MaxMaterializedTaskLogChars - materializedChars)
    {
      edit->setPlainText(materializedTaskLog(text, TaskLogTrimTargetChars));
      replacedDocument = true;
    }
    else
    {
      QTextCursor cursor = edit->textCursor();
      cursor.movePosition(QTextCursor::End);
      cursor.insertText(text.mid(renderedSourceChars));
    }
  }

  renderedSourceChars = text.size();
  renderedRevision = revision;
  if(followTail)
  {
    QTextCursor cursor = edit->textCursor();
    cursor.movePosition(QTextCursor::End);
    edit->setTextCursor(cursor);
  }
  else if(replacedDocument && scroll)
  {
    scroll->setValue(qMin(oldScrollValue, scroll->maximum()));
  }
}
}    // namespace

// Reusable, read-only detail surface shared by the main monitor and detached Agent windows.
class AIAgentTraceTabs : public QWidget
{
public:
  explicit AIAgentTraceTabs(AIAgentTraceStore &store, QWidget *parent = NULL)
      : QWidget(parent), m_Store(store)
  {
    QVBoxLayout *layout = new QVBoxLayout(this);
    layout->setContentsMargins(0, 0, 0, 0);
    layout->setSpacing(4);

    m_Title = new QLabel(this);
    m_Title->setTextInteractionFlags(Qt::TextSelectableByMouse);
    QFont titleFont = m_Title->font();
    titleFont.setBold(true);
    m_Title->setFont(titleFont);
    layout->addWidget(m_Title);

    m_Metadata = new QLabel(this);
    m_Metadata->setTextInteractionFlags(Qt::TextSelectableByMouse);
    m_Metadata->setWordWrap(true);
    layout->addWidget(m_Metadata);

    QLabel *notice = new QLabel(
        QCoreApplication::translate(
            "AIAgentMonitorWindow",
            "Only reasoning exposed by the provider can be shown. Raw traces may contain "
            "sensitive data and are cached locally."),
        this);
    notice->setWordWrap(true);
    layout->addWidget(notice);

    m_Tabs = new QTabWidget(this);
    m_TaskLog = new QPlainTextEdit(m_Tabs);
    m_RawReasoning = new QPlainTextEdit(m_Tabs);
    m_Summary = new QPlainTextEdit(m_Tabs);
    m_Messages = new QPlainTextEdit(m_Tabs);
    m_ToolsStatus = new QPlainTextEdit(m_Tabs);
    m_Cache = new QPlainTextEdit(m_Tabs);

    configureTextView(
        m_TaskLog,
        QCoreApplication::translate("AIAgentMonitorWindow",
                                    "No task events have been received for this task."),
        false);
    configureTextView(
        m_RawReasoning,
        QCoreApplication::translate("AIAgentMonitorWindow",
                                    "No raw reasoning has been received for this Agent."),
        true);
    configureTextView(
        m_Summary,
        QCoreApplication::translate("AIAgentMonitorWindow",
                                    "No reasoning summary has been received for this Agent."),
        true);
    configureTextView(
        m_Messages,
        QCoreApplication::translate("AIAgentMonitorWindow",
                                    "No message content has been received for this Agent."),
        true);
    configureTextView(
        m_ToolsStatus,
        QCoreApplication::translate("AIAgentMonitorWindow",
                                    "No tool or status activity has been received for this Agent."),
        false);
    configureTextView(m_Cache,
                      QCoreApplication::translate(
                          "AIAgentMonitorWindow",
                          "No cache or context metadata has been received for this Agent."),
                      false);

    m_Tabs->addTab(m_TaskLog, QCoreApplication::translate("AIAgentMonitorWindow", "Task Log"));
    m_Tabs->addTab(m_RawReasoning,
                   QCoreApplication::translate("AIAgentMonitorWindow", "Raw Reasoning"));
    m_Tabs->addTab(m_Summary,
                   QCoreApplication::translate("AIAgentMonitorWindow", "Reasoning Summary"));
    m_Tabs->addTab(m_Messages, QCoreApplication::translate("AIAgentMonitorWindow", "Messages"));
    m_Tabs->addTab(m_ToolsStatus,
                   QCoreApplication::translate("AIAgentMonitorWindow", "Tools && Status"));
    m_Tabs->addTab(m_Cache,
                   QCoreApplication::translate("AIAgentMonitorWindow", "Cache && Context"));
    connect(m_Tabs, &QTabWidget::currentChanged, this, [this](int) { Refresh(); });
    layout->addWidget(m_Tabs, 1);
    Refresh();
  }

  void SetAgent(const QString &taskId, const QString &agentId)
  {
    if(m_TaskId == taskId && m_AgentId == agentId)
      return;
    m_TaskId = taskId;
    m_AgentId = agentId;
    m_RenderedTaskLogChars = 0;
    m_RenderedRawChars = 0;
    m_RenderedSummaryChars = 0;
    m_RenderedMessagesChars = 0;
    m_RenderedTaskLogRevision = 0;
    m_RenderedRawRevision = 0;
    m_RenderedSummaryRevision = 0;
    m_RenderedMessagesRevision = 0;
    m_RenderedTools.clear();
    m_RenderedCache.clear();
    m_TaskLog->clear();
    m_RawReasoning->clear();
    m_Summary->clear();
    m_Messages->clear();
    m_ToolsStatus->clear();
    m_Cache->clear();
    Refresh();
  }

  void Refresh()
  {
    const AIAgentTraceTask *task = m_Store.Task(m_TaskId);
    const AIAgentTraceAgent *agent = m_Store.Agent(m_TaskId, m_AgentId);
    QWidget *visibleTrace = m_Tabs->currentWidget();
    if(visibleTrace == m_TaskLog)
      updateTaskLogView(m_TaskLog, m_RenderedTaskLogChars, m_RenderedTaskLogRevision,
                        task ? task->eventLog : QString(), task ? task->eventLogRevision : 0);
    if(!agent)
    {
      m_Title->setText(QCoreApplication::translate("AIAgentMonitorWindow", "No Agent selected"));
      m_Metadata->clear();
      m_Tabs->setEnabled(task != NULL);
      m_RawReasoning->setPlaceholderText(rawReasoningPlaceholder(NULL));
      if(visibleTrace == m_RawReasoning)
        updateStreamingTextView(m_RawReasoning, m_RenderedRawChars, m_RenderedRawRevision,
                                QString(), 0);
      else if(visibleTrace == m_Summary)
        updateStreamingTextView(m_Summary, m_RenderedSummaryChars, m_RenderedSummaryRevision,
                                QString(), 0);
      else if(visibleTrace == m_Messages)
        updateStreamingTextView(m_Messages, m_RenderedMessagesChars, m_RenderedMessagesRevision,
                                QString(), 0);
      else if(visibleTrace == m_ToolsStatus)
        updateTextView(m_ToolsStatus, m_RenderedTools, QString());
      else if(visibleTrace == m_Cache)
        updateTextView(m_Cache, m_RenderedCache, QString());
      return;
    }

    m_Tabs->setEnabled(true);
    m_RawReasoning->setPlaceholderText(rawReasoningPlaceholder(agent));
    m_Title->setText(roleText(*agent));
    QStringList metadata;
    metadata.push_back(QCoreApplication::translate("AIAgentMonitorWindow", "Task: %1").arg(m_TaskId));
    metadata.push_back(
        QCoreApplication::translate("AIAgentMonitorWindow", "Agent ID: %1").arg(agent->id));
    if(!agent->parentId.isEmpty())
      metadata.push_back(
          QCoreApplication::translate("AIAgentMonitorWindow", "Parent Agent: %1").arg(agent->parentId));
    if(!agent->status.isEmpty())
      metadata.push_back(QCoreApplication::translate("AIAgentMonitorWindow", "Status: %1")
                             .arg(statusText(agent->status)));
    const QString lastEventTime =
        agent->lastTimestampMs > 0
            ? QLocale().toString(QDateTime::fromMSecsSinceEpoch(agent->lastTimestampMs),
                                 QLocale::ShortFormat)
            : QCoreApplication::translate("AIAgentMonitorWindow", "Unknown");
    metadata.push_back(
        QCoreApplication::translate("AIAgentMonitorWindow", "Last event time: %1").arg(lastEventTime));
    if(!agent->lastEventKind.isEmpty())
      metadata.push_back(QCoreApplication::translate("AIAgentMonitorWindow", "Last event kind: %1")
                             .arg(agent->lastEventKind));
    const QString lifecycle =
        agent->rawReasoningLifecycle.isEmpty()
            ? QCoreApplication::translate("AIAgentMonitorWindow", "Not reported")
            : lifecycleText(agent->rawReasoningLifecycle);
    metadata.push_back(
        QCoreApplication::translate("AIAgentMonitorWindow", "Raw reasoning lifecycle: %1")
            .arg(lifecycle));
    const QString availability =
        agent->rawReasoningAvailability.isEmpty()
            ? QCoreApplication::translate("AIAgentMonitorWindow", "Not reported")
            : availabilityText(agent->rawReasoningAvailability);
    metadata.push_back(
        QCoreApplication::translate("AIAgentMonitorWindow", "Raw reasoning availability: %1")
            .arg(availability));
    if(!agent->model.isEmpty())
      metadata.push_back(
          QCoreApplication::translate("AIAgentMonitorWindow", "Model: %1").arg(agent->model));
    if(!agent->profile.isEmpty())
      metadata.push_back(
          QCoreApplication::translate("AIAgentMonitorWindow", "Connection: %1").arg(agent->profile));
    // i18n-ignore: Visual separator between complete, independently translated metadata fields.
    m_Metadata->setText(metadata.join(QStringLiteral("  |  ")));

    // The store keeps every stream current. Materialise only the visible QTextDocument; inactive
    // tabs catch up immediately when selected instead of performing six layouts per event burst.
    if(visibleTrace == m_RawReasoning)
      updateStreamingTextView(m_RawReasoning, m_RenderedRawChars, m_RenderedRawRevision,
                              agent->rawReasoning, agent->rawReasoningRevision);
    else if(visibleTrace == m_Summary)
      updateStreamingTextView(m_Summary, m_RenderedSummaryChars, m_RenderedSummaryRevision,
                              agent->reasoningSummary, agent->reasoningSummaryRevision);
    else if(visibleTrace == m_Messages)
      updateStreamingTextView(m_Messages, m_RenderedMessagesChars, m_RenderedMessagesRevision,
                              agent->messages, agent->messagesRevision);
    else if(visibleTrace == m_ToolsStatus)
      updateTextView(m_ToolsStatus, m_RenderedTools, agent->toolsAndStatus);
    else if(visibleTrace == m_Cache)
      updateTextView(m_Cache, m_RenderedCache, agent->cacheMetadata);
  }

private:
  AIAgentTraceStore &m_Store;
  QString m_TaskId;
  QString m_AgentId;
  QLabel *m_Title = NULL;
  QLabel *m_Metadata = NULL;
  QTabWidget *m_Tabs = NULL;
  QPlainTextEdit *m_TaskLog = NULL;
  QPlainTextEdit *m_RawReasoning = NULL;
  QPlainTextEdit *m_Summary = NULL;
  QPlainTextEdit *m_Messages = NULL;
  QPlainTextEdit *m_ToolsStatus = NULL;
  QPlainTextEdit *m_Cache = NULL;
  int m_RenderedTaskLogChars = 0;
  int m_RenderedRawChars = 0;
  int m_RenderedSummaryChars = 0;
  int m_RenderedMessagesChars = 0;
  quint64 m_RenderedTaskLogRevision = 0;
  quint64 m_RenderedRawRevision = 0;
  quint64 m_RenderedSummaryRevision = 0;
  quint64 m_RenderedMessagesRevision = 0;
  QString m_RenderedTools;
  QString m_RenderedCache;
};

namespace
{
class AIAgentDetailWindow : public QFrame
{
public:
  AIAgentDetailWindow(AIAgentTraceStore &store, const QString &taskId, const QString &agentId)
      : QFrame(NULL, Qt::Tool), m_Store(store), m_TaskId(taskId), m_AgentId(agentId)
  {
    // Detached views can hold several large QTextDocuments. Release them on close; the QPointer
    // registry below will transparently create a fresh window when the Agent is opened again.
    setAttribute(Qt::WA_DeleteOnClose, true);
    setMinimumSize(QSize(760, 480));
    QVBoxLayout *layout = new QVBoxLayout(this);
    layout->setContentsMargins(7, 7, 7, 7);
    m_Tabs = new AIAgentTraceTabs(m_Store, this);
    m_Tabs->SetAgent(m_TaskId, m_AgentId);
    layout->addWidget(m_Tabs);

    m_RefreshTimer = new QTimer(this);
    m_RefreshTimer->setSingleShot(true);
    m_RefreshTimer->setInterval(60);
    QObject::connect(m_RefreshTimer, &QTimer::timeout, this, [this]() { Refresh(); });
    m_ObserverId = m_Store.AddObserver([this](const QString &taskId, const QString &agentId) {
      if(isVisible() && (taskId.isEmpty() || (taskId == m_TaskId && agentId == m_AgentId)) &&
         !m_RefreshTimer->isActive())
        m_RefreshTimer->start();
    });
    Refresh();
  }

  ~AIAgentDetailWindow() { m_Store.RemoveObserver(m_ObserverId); }

  void Open()
  {
    Refresh();
    show();
    raise();
    activateWindow();
  }

private:
  void Refresh()
  {
    const AIAgentTraceAgent *agent = m_Store.Agent(m_TaskId, m_AgentId);
    const QString name = agent ? roleText(*agent) : m_AgentId;
    setWindowTitle(
        QCoreApplication::translate("AIAgentMonitorWindow", "%1 - Agent Monitor").arg(name));
    m_Tabs->Refresh();
  }

  AIAgentTraceStore &m_Store;
  QString m_TaskId;
  QString m_AgentId;
  quint64 m_ObserverId = 0;
  AIAgentTraceTabs *m_Tabs = NULL;
  QTimer *m_RefreshTimer = NULL;
};
}    // namespace

AIAgentMonitorWindow::AIAgentMonitorWindow(AIAgentTraceStore &store, QWidget *parent)
    : QFrame(parent, Qt::Tool), m_Store(store)
{
  setAttribute(Qt::WA_DeleteOnClose, false);
  setWindowTitle(QCoreApplication::translate("AIAgentMonitorWindow", "Agent Monitor"));
  setMinimumSize(QSize(900, 560));

  QVBoxLayout *root = new QVBoxLayout(this);
  root->setContentsMargins(7, 7, 7, 7);
  root->setSpacing(5);

  QHBoxLayout *taskRow = new QHBoxLayout();
  taskRow->setContentsMargins(0, 0, 0, 0);
  taskRow->addWidget(new QLabel(QCoreApplication::translate("AIAgentMonitorWindow", "Task:"), this));
  m_TaskCombo = new QComboBox(this);
  m_TaskCombo->setMinimumContentsLength(24);
  m_TaskCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
  taskRow->addWidget(m_TaskCombo, 1);
  QPushButton *loadTask =
      new QPushButton(QCoreApplication::translate("AIAgentMonitorWindow", "Open Task Log..."), this);
  taskRow->addWidget(loadTask);
  root->addLayout(taskRow);

  m_Status = new QLabel(this);
  m_Status->setTextInteractionFlags(Qt::TextSelectableByMouse);
  root->addWidget(m_Status);

  QSplitter *splitter = new QSplitter(Qt::Horizontal, this);
  QWidget *agentPanel = new QWidget(splitter);
  QVBoxLayout *agentLayout = new QVBoxLayout(agentPanel);
  agentLayout->setContentsMargins(0, 0, 0, 0);
  agentLayout->setSpacing(4);
  m_AgentList = new QTreeWidget(agentPanel);
  m_AgentList->setColumnCount(2);
  m_AgentList->setHeaderLabels(QStringList()
                               << QCoreApplication::translate("AIAgentMonitorWindow", "Agent")
                               << QCoreApplication::translate("AIAgentMonitorWindow", "Status"));
  m_AgentList->setRootIsDecorated(true);
  m_AgentList->setUniformRowHeights(true);
  agentLayout->addWidget(m_AgentList, 1);
  m_OpenAgent = new QPushButton(
      QCoreApplication::translate("AIAgentMonitorWindow", "Open Selected Agent Window"), agentPanel);
  m_OpenAgent->setEnabled(false);
  agentLayout->addWidget(m_OpenAgent);
  splitter->addWidget(agentPanel);

  m_TraceTabs = new AIAgentTraceTabs(m_Store, splitter);
  splitter->addWidget(m_TraceTabs);
  splitter->setStretchFactor(0, 0);
  splitter->setStretchFactor(1, 1);
  splitter->setSizes(QList<int>() << 250 << 650);
  root->addWidget(splitter, 1);

  m_RefreshTimer = new QTimer(this);
  m_RefreshTimer->setSingleShot(true);
  m_RefreshTimer->setInterval(60);
  connect(m_RefreshTimer, &QTimer::timeout, this, [this]() { RefreshFromStore(); });
  m_TaskTailTimer = new QTimer(this);
  m_TaskTailTimer->setInterval(150);
  connect(m_TaskTailTimer, &QTimer::timeout, this, [this]() { PollTaskFile(); });
  connect(m_TaskCombo, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged), this,
          [this](int index) { SelectTask(index); });
  connect(m_AgentList, &QTreeWidget::currentItemChanged, this,
          [this](QTreeWidgetItem *current, QTreeWidgetItem *) { SelectAgent(current); });
  connect(loadTask, &QPushButton::clicked, this, [this]() { ChooseTaskFile(); });
  connect(m_OpenAgent, &QPushButton::clicked, this, [this]() { OpenSelectedAgentWindow(); });

  m_ObserverId = m_Store.AddObserver([this](const QString &, const QString &) {
    if(isVisible())
      ScheduleRefresh();
  });
  RefreshFromStore();
}

AIAgentMonitorWindow::~AIAgentMonitorWindow()
{
  m_Store.RemoveObserver(m_ObserverId);
  const QList<QPointer<QWidget>> windows = m_AgentWindows.values();
  m_AgentWindows.clear();
  for(const QPointer<QWidget> &window : windows)
    delete window.data();
}

void AIAgentMonitorWindow::Open()
{
  show();
  if(m_TaskTailTimer && !m_TaskTailTimer->isActive())
    m_TaskTailTimer->start();
  PollTaskFile();
  m_LastStructureRefreshMs = 0;
  RefreshFromStore();
  raise();
  activateWindow();
}

void AIAgentMonitorWindow::OpenEmpty()
{
  m_ExplicitNoTask = true;
  m_RequestedTaskId.clear();
  m_SelectedAgentId.clear();
  StopTailingTask();
  if(m_TaskTailTimer)
    m_TaskTailTimer->stop();
  m_TaskCombo->blockSignals(true);
  m_TaskCombo->clear();
  m_TaskCombo->blockSignals(false);
  m_AgentList->clear();
  m_OpenAgent->setEnabled(false);
  m_TraceTabs->SetAgent(QString(), QString());
  m_TraceTabs->Refresh();
  m_Status->setText(QCoreApplication::translate(
      "AIAgentMonitorWindow", "This conversation does not have an AI task log yet."));
  show();
  raise();
  activateWindow();
}

void AIAgentMonitorWindow::HideAll()
{
  if(m_TaskTailTimer)
    m_TaskTailTimer->stop();
  hide();
  for(const QPointer<QWidget> &window : m_AgentWindows)
    if(window)
      window->hide();
}

void AIAgentMonitorWindow::SetTaskEventRoot(const QString &eventRoot)
{
  m_EventRoot = eventRoot;
  RefreshTasks();
}

bool AIAgentMonitorWindow::OpenTask(const QString &taskId, QString *error)
{
  if(!safeTaskId(taskId))
  {
    const QString message =
        QCoreApplication::translate("AIAgentMonitorWindow", "The task identifier is not valid.");
    if(error)
      *error = message;
    m_Status->setText(message);
    return false;
  }
  if(CurrentTaskId() != taskId)
    m_SelectedAgentId.clear();
  m_ExplicitNoTask = false;
  m_RequestedTaskId = taskId;
  QString replayWarning;
  const QString filePath = TaskFilePath(taskId);
  if(!filePath.isEmpty())
  {
    const bool alreadyLoaded = m_Store.Task(taskId) != NULL;
    const QFileInfo logFile(filePath);
    const bool replayIncrementally = !alreadyLoaded && logFile.exists() &&
                                     logFile.size() > MaxSynchronousReplayBytes &&
                                     logFile.size() <= MaxInitialTailHistoryBytes;
    const QString absoluteFilePath = QFileInfo(filePath).absoluteFilePath();
    if(m_TailedTaskId != taskId || m_TailedFilePath != absoluteFilePath)
      BeginTailingTask(taskId, filePath, replayIncrementally);
    // The Assistant feeds the same live task into the shared store. Replaying an already-loaded
    // 10-30 MiB JSONL here would validate and parse it twice on the button's GUI-thread stack.
    // Unloaded histories over 1 MiB are tailed incrementally so opening the monitor never parses a
    // large file twice in one GUI callback.
    if(!alreadyLoaded && logFile.exists() && logFile.size() <= MaxSynchronousReplayBytes)
    {
      if(m_TaskFiles.contains(taskId))
      {
        QString replayedTaskId;
        if(!m_Store.ReplayTaskFile(filePath, &replayWarning, &replayedTaskId) ||
           replayedTaskId != taskId)
        {
          if(replayWarning.isEmpty())
            replayWarning = QCoreApplication::translate("AIAgentTraceStore",
                                                        "The task log does not belong to task %1.")
                                .arg(taskId);
        }
      }
      else
      {
        m_Store.ReplayTaskId(taskId, m_EventRoot, &replayWarning);
      }
    }
  }
  RefreshTasks();
  Open();
  if(error)
    *error = replayWarning;
  if(!replayWarning.isEmpty())
    m_Status->setText(replayWarning);
  return true;
}

bool AIAgentMonitorWindow::ReplayTaskFile(const QString &filePath, QString *error)
{
  QString replayError;
  QString taskId;
  if(!m_Store.ReplayTaskFile(filePath, &replayError, &taskId))
  {
    if(error)
      *error = replayError;
    m_Status->setText(replayError);
    return false;
  }
  if(safeTaskId(taskId) && m_Store.Task(taskId))
  {
    m_ExplicitNoTask = false;
    m_RequestedTaskId = taskId;
    m_TaskFiles.insert(taskId, QFileInfo(filePath).absoluteFilePath());
    // Manual logs reveal their embedded task id only after validation. Re-read from byte zero;
    // Store sequence de-duplication makes this safe and closes the Replay-to-tail race.
    BeginTailingTask(taskId, m_TaskFiles.value(taskId), true);
  }
  if(error)
    *error = replayError;
  RefreshFromStore();
  m_Status->setText(
      replayError.isEmpty()
          ? QCoreApplication::translate("AIAgentMonitorWindow", "Loaded task log: %1").arg(filePath)
          : replayError);
  return true;
}

QString AIAgentMonitorWindow::CurrentTaskId() const
{
  return m_TaskCombo && m_TaskCombo->currentIndex() >= 0
             ? m_TaskCombo->currentData(Qt::UserRole).toString()
             : QString();
}

QString AIAgentMonitorWindow::CurrentAgentId() const
{
  return m_SelectedAgentId;
}

void AIAgentMonitorWindow::closeEvent(QCloseEvent *event)
{
  event->ignore();
  if(m_TaskTailTimer)
    m_TaskTailTimer->stop();
  hide();
}

void AIAgentMonitorWindow::ScheduleRefresh()
{
  if(m_RefreshTimer && !m_RefreshTimer->isActive())
    m_RefreshTimer->start();
}

void AIAgentMonitorWindow::RefreshFromStore()
{
  if(m_ExplicitNoTask)
  {
    m_Status->setText(QCoreApplication::translate(
        "AIAgentMonitorWindow", "This conversation does not have an AI task log yet."));
    return;
  }

  const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
  const qint64 structureAgeMs = nowMs - m_LastStructureRefreshMs;
  if(m_LastStructureRefreshMs <= 0 || structureAgeMs >= 1000)
  {
    // Task discovery sorts the log directory and RefreshAgents rebuilds the complete tree. Token
    // events only need the selected trace text, so keep structural work on a slower cadence.
    RefreshTasks();
    RefreshAgents();
    m_LastStructureRefreshMs = nowMs;
  }
  else
  {
    m_TraceTabs->SetAgent(CurrentTaskId(), m_SelectedAgentId);
    m_TraceTabs->Refresh();
    // Guarantee that a newly-created task/Agent appears even if no later event arrives.
    if(isVisible() && m_RefreshTimer && !m_RefreshTimer->isActive())
      m_RefreshTimer->start(int(qMax<qint64>(60, 1000 - structureAgeMs)));
  }
}

void AIAgentMonitorWindow::RefreshTasks()
{
  if(m_ExplicitNoTask)
  {
    m_TaskCombo->blockSignals(true);
    m_TaskCombo->clear();
    m_TaskCombo->blockSignals(false);
    return;
  }
  if(!m_EventRoot.isEmpty())
    m_Store.PurgeExpiredArchivedTaskLogs(m_EventRoot, 7);
  const QString previous = !m_RequestedTaskId.isEmpty() ? m_RequestedTaskId : CurrentTaskId();
  const QStringList loadedTaskIds = m_Store.TaskIds();
  QStringList taskIds = loadedTaskIds;
  const QStringList discovered = m_Store.DiscoverTaskIds(m_EventRoot);
  for(const QString &taskId : discovered)
    if(!taskIds.contains(taskId))
      taskIds.push_back(taskId);
  if(!previous.isEmpty() && !taskIds.contains(previous))
    taskIds.prepend(previous);

  m_TaskCombo->blockSignals(true);
  m_TaskCombo->clear();
  for(const QString &taskId : taskIds)
  {
    QString label = taskId;
    if(m_Store.IsTaskLogArchived(taskId, m_EventRoot))
      label = QCoreApplication::translate("AIAgentMonitorWindow", "%1 (archived)").arg(taskId);
    m_TaskCombo->addItem(label, taskId);
  }

  int selection = taskIds.indexOf(previous);
  if(selection < 0 && !loadedTaskIds.isEmpty())
    selection = taskIds.indexOf(loadedTaskIds.last());
  if(selection < 0 && !taskIds.isEmpty())
    selection = 0;
  if(selection >= 0)
    m_TaskCombo->setCurrentIndex(selection);
  m_TaskCombo->blockSignals(false);
  m_RequestedTaskId.clear();
}

void AIAgentMonitorWindow::RefreshAgents()
{
  const QString taskId = CurrentTaskId();
  const QString previous = m_SelectedAgentId;
  QString replayError;
  const AIAgentTraceTask *task = m_Store.Task(taskId);
  const QString replayPath = TaskFilePath(taskId);
  if(!task && !taskId.isEmpty() && !replayPath.isEmpty() && QFileInfo::exists(replayPath) &&
     QFileInfo(replayPath).size() <= MaxSynchronousReplayBytes)
  {
    if(m_TaskFiles.contains(taskId))
    {
      QString replayedTaskId;
      m_Store.ReplayTaskFile(m_TaskFiles.value(taskId), &replayError, &replayedTaskId);
      if(!replayedTaskId.isEmpty() && replayedTaskId != taskId)
        replayError = QCoreApplication::translate("AIAgentTraceStore",
                                                  "The task log does not belong to task %1.")
                          .arg(taskId);
    }
    else
    {
      m_Store.ReplayTaskId(taskId, m_EventRoot, &replayError);
    }
    task = m_Store.Task(taskId);
  }

  m_AgentList->blockSignals(true);
  m_AgentList->clear();
  QHash<QString, QTreeWidgetItem *> items;
  QTreeWidgetItem *mainItem = NULL;
  QTreeWidgetItem *reasoningItem = NULL;
  QTreeWidgetItem *summaryItem = NULL;
  if(task)
  {
    for(const QString &agentId : task->agentOrder)
    {
      const AIAgentTraceAgent *agent = m_Store.Agent(taskId, agentId);
      if(!agent)
        continue;
      QTreeWidgetItem *item = new QTreeWidgetItem();
      item->setText(0, roleText(*agent));
      item->setText(1, statusText(agent->status));
      item->setData(0, Qt::UserRole, agent->id);
      item->setToolTip(
          0, QCoreApplication::translate("AIAgentMonitorWindow", "Agent ID: %1").arg(agent->id));
      items.insert(agent->id, item);
      // i18n-ignore: Agent role is a stable ReplayLab protocol identifier.
      if(agent->role == QLatin1String("main") && !mainItem)
        mainItem = item;
      if(!reasoningItem && !agent->rawReasoning.isEmpty())
        reasoningItem = item;
      if(!summaryItem && (!agent->reasoningSummary.isEmpty() || !agent->messages.isEmpty()))
        summaryItem = item;
    }

    for(const QString &agentId : task->agentOrder)
    {
      const AIAgentTraceAgent *agent = m_Store.Agent(taskId, agentId);
      QTreeWidgetItem *item = items.value(agentId, NULL);
      if(!agent || !item)
        continue;
      bool parentCycle = false;
      QSet<QString> ancestors;
      ancestors.insert(agent->id);
      QString parentId = agent->parentId;
      while(!parentId.isEmpty())
      {
        if(ancestors.contains(parentId))
        {
          parentCycle = true;
          break;
        }
        ancestors.insert(parentId);
        const AIAgentTraceAgent *parent = m_Store.Agent(taskId, parentId);
        if(!parent)
          break;
        parentId = parent->parentId;
      }
      QTreeWidgetItem *parentItem = parentCycle ? NULL : items.value(agent->parentId, NULL);
      if(!parentCycle && !parentItem && agent->role == QLatin1String("child"))
        parentItem = mainItem;
      if(parentItem && parentItem != item)
        parentItem->addChild(item);
      else
        m_AgentList->addTopLevelItem(item);
    }
  }
  m_AgentList->expandAll();

  QTreeWidgetItem *selection = items.value(previous, NULL);
  if(!selection && reasoningItem)
    selection = reasoningItem;
  if(!selection && summaryItem)
    selection = summaryItem;
  if(!selection && mainItem)
    selection = mainItem;
  if(!selection && m_AgentList->topLevelItemCount() > 0)
    selection = m_AgentList->topLevelItem(0);
  if(selection)
    m_AgentList->setCurrentItem(selection);
  else
    m_SelectedAgentId.clear();
  m_AgentList->blockSignals(false);

  SelectAgent(selection);
  if(!taskId.isEmpty() && !task)
    m_Status->setText(
        replayError.isEmpty()
            ? QCoreApplication::translate("AIAgentMonitorWindow", "Waiting for task events in: %1")
                  .arg(TaskFilePath(taskId))
            : replayError);
  else if(task && task->agents.isEmpty())
    m_Status->setText(QCoreApplication::translate(
        "AIAgentMonitorWindow", "No Agent events have been received for this task."));
  else if(task)
    m_Status->setText(QCoreApplication::translate("AIAgentMonitorWindow",
                                                  "Monitoring task log: %1\n%2 Agent(s) recorded.")
                          .arg(TaskFilePath(taskId))
                          .arg(task->agents.size()));
  else
    m_Status->setText(QCoreApplication::translate("AIAgentMonitorWindow",
                                                  "No task logs are loaded or available."));
}

void AIAgentMonitorWindow::SelectTask(int index)
{
  if(index < 0)
  {
    RefreshAgents();
    return;
  }
  m_ExplicitNoTask = false;
  const QString taskId = m_TaskCombo->itemData(index, Qt::UserRole).toString();
  QString replayNotice;
  const QString filePath = TaskFilePath(taskId);
  if(!filePath.isEmpty())
  {
    const bool alreadyLoaded = m_Store.Task(taskId) != NULL;
    const QFileInfo logFile(filePath);
    const bool replayIncrementally = !alreadyLoaded && logFile.exists() &&
                                     logFile.size() > MaxSynchronousReplayBytes &&
                                     logFile.size() <= MaxInitialTailHistoryBytes;
    BeginTailingTask(taskId, filePath, replayIncrementally);
    if(!alreadyLoaded && logFile.exists() && logFile.size() <= MaxSynchronousReplayBytes)
    {
      if(m_TaskFiles.contains(taskId))
      {
        QString replayedTaskId;
        if(!m_Store.ReplayTaskFile(filePath, &replayNotice, &replayedTaskId) ||
           replayedTaskId != taskId)
        {
          if(replayNotice.isEmpty())
            replayNotice = QCoreApplication::translate("AIAgentTraceStore",
                                                       "The task log does not belong to task %1.")
                               .arg(taskId);
        }
      }
      else
      {
        m_Store.ReplayTaskId(taskId, m_EventRoot, &replayNotice);
      }
    }
  }
  m_SelectedAgentId.clear();
  RefreshAgents();
  if(!replayNotice.isEmpty())
    m_Status->setText(replayNotice);
}

void AIAgentMonitorWindow::SelectAgent(QTreeWidgetItem *item)
{
  m_SelectedAgentId = item ? item->data(0, Qt::UserRole).toString() : QString();
  m_OpenAgent->setEnabled(!CurrentTaskId().isEmpty() && !m_SelectedAgentId.isEmpty());
  m_TraceTabs->SetAgent(CurrentTaskId(), m_SelectedAgentId);
  m_TraceTabs->Refresh();
}

void AIAgentMonitorWindow::ChooseTaskFile()
{
  const QString filePath = QFileDialog::getOpenFileName(
      this, QCoreApplication::translate("AIAgentMonitorWindow", "Open Task Log"),
      m_EventRoot,
      QCoreApplication::translate("AIAgentMonitorWindow",
                                  "Task logs (*.jsonl);;All files (*)"));
  if(!filePath.isEmpty())
    ReplayTaskFile(filePath);
}

void AIAgentMonitorWindow::OpenSelectedAgentWindow()
{
  const QString taskId = CurrentTaskId();
  const QString agentId = CurrentAgentId();
  if(taskId.isEmpty() || agentId.isEmpty())
    return;
  const QString key = taskId + QChar(0x1f) + agentId;
  AIAgentDetailWindow *window = NULL;
  if(m_AgentWindows.contains(key) && !m_AgentWindows.value(key).isNull())
    window = static_cast<AIAgentDetailWindow *>(m_AgentWindows.value(key).data());
  else
  {
    window = new AIAgentDetailWindow(m_Store, taskId, agentId);
    m_AgentWindows.insert(key, window);
  }
  window->Open();
}

void AIAgentMonitorWindow::ForgetTask(const QString &taskId)
{
  if(m_TailedTaskId == taskId)
    StopTailingTask();
  if(m_RequestedTaskId == taskId)
    m_RequestedTaskId.clear();
  m_TaskFiles.remove(taskId);
  const QString prefix = taskId + QChar(0x1f);
  const QStringList windows = m_AgentWindows.keys();
  for(const QString &key : windows)
    if(key.startsWith(prefix))
      delete m_AgentWindows.take(key).data();
  const bool selected = CurrentTaskId() == taskId;
  const QSignalBlocker blocker(m_TaskCombo);
  for(int i = m_TaskCombo->count() - 1; i >= 0; --i)
    if(m_TaskCombo->itemData(i).toString() == taskId)
      m_TaskCombo->removeItem(i);
  if(selected)
  {
    m_SelectedAgentId.clear();
    m_TraceTabs->SetAgent(QString(), QString());
  }
  m_LastStructureRefreshMs = 0;
  RefreshFromStore();
}

QString AIAgentMonitorWindow::TaskFilePath(const QString &taskId) const
{
  const QString explicitPath = m_TaskFiles.value(taskId);
  if(!explicitPath.isEmpty())
    return explicitPath;
  if(taskId.isEmpty() || m_EventRoot.isEmpty())
    return QString();
  return QDir(m_EventRoot).filePath(taskId + QStringLiteral(".jsonl"));
}

void AIAgentMonitorWindow::StopTailingTask()
{
  m_TailedTaskId.clear();
  m_TailedFilePath.clear();
  m_TailOffset = 0;
  m_TailBirthMs = 0;
  m_TailBytes.clear();
  m_DiscardingTailLine = false;
}

void AIAgentMonitorWindow::BeginTailingTask(const QString &taskId, const QString &filePath,
                                            bool replayFromStart)
{
  m_TailedTaskId = taskId;
  m_TailedFilePath = QFileInfo(filePath).absoluteFilePath();
  m_TailBytes.clear();
  m_DiscardingTailLine = false;
  m_TailOffset = 0;
  m_TailBirthMs = 0;
  const QFileInfo fileInfo(m_TailedFilePath);
  if(!fileInfo.exists() || fileInfo.isSymLink() || !fileInfo.isFile())
    return;
  if(fileInfo.created().isValid())
    m_TailBirthMs = fileInfo.created().toMSecsSinceEpoch();
  if(replayFromStart)
    return;

  QFile file(m_TailedFilePath);
  if(!file.open(QIODevice::ReadOnly))
    return;
  if(file.size() > MaxInitialTailHistoryBytes)
  {
    // Initial replay is bounded, but live monitoring is not tied to total file size. Start within
    // the latest history window and discard the first partial line at that byte boundary.
    m_TailOffset = file.size() - MaxInitialTailHistoryBytes;
    m_DiscardingTailLine = m_TailOffset > 0;
    return;
  }

  // The caller captured this boundary before full replay. Re-read the trailing partial line and
  // every later append; sequence de-duplication removes overlap with replay without any loss.
  const qint64 scanStart = qMax(qint64(0), file.size() - qint64(MaxTailEventLineBytes) - qint64(1));
  if(!file.seek(scanStart))
    return;
  const QByteArray suffix = file.read(file.size() - scanStart);
  const int newline = suffix.lastIndexOf('\n');
  if(newline >= 0)
    m_TailOffset = scanStart + newline + 1;
  else if(scanStart > 0)
  {
    m_TailOffset = scanStart;
    m_DiscardingTailLine = true;
  }
}

void AIAgentMonitorWindow::PollTaskFile()
{
  if(!isVisible() || m_TailedTaskId.isEmpty() || m_TailedFilePath.isEmpty())
    return;
  const QFileInfo fileInfo(m_TailedFilePath);
  if(!fileInfo.exists())
    return;
  if(fileInfo.isSymLink() || !fileInfo.isFile())
  {
    m_Status->setText(QCoreApplication::translate("AIAgentTraceStore",
                                                  "The task log path is not a regular file."));
    m_TailedTaskId.clear();
    m_TailedFilePath.clear();
    return;
  }
  const qint64 birthMs = fileInfo.created().isValid() ? fileInfo.created().toMSecsSinceEpoch() : 0;
  if(m_TailBirthMs > 0 && birthMs > 0 && birthMs != m_TailBirthMs)
  {
    m_Status->setText(
        QCoreApplication::translate("AIAgentMonitorWindow",
                                    "The task log was replaced or truncated. Live monitoring "
                                    "stopped to avoid mixing task histories."));
    m_TailedTaskId.clear();
    m_TailedFilePath.clear();
    return;
  }
  if(m_TailBirthMs <= 0 && birthMs > 0)
    m_TailBirthMs = birthMs;
  QFile file(m_TailedFilePath);
  if(!file.open(QIODevice::ReadOnly))
    return;
  if(file.size() < m_TailOffset)
  {
    m_Status->setText(
        QCoreApplication::translate("AIAgentMonitorWindow",
                                    "The task log was replaced or truncated. Live monitoring "
                                    "stopped to avoid mixing task histories."));
    m_TailedTaskId.clear();
    m_TailedFilePath.clear();
    return;
  }
  if(file.size() == m_TailOffset || !file.seek(m_TailOffset))
    return;

  QByteArray chunk = file.read(qMin(MaxTailReadBytes, file.size() - m_TailOffset));
  m_TailOffset = file.pos();
  if(chunk.isEmpty())
    return;
  if(m_DiscardingTailLine)
  {
    const int newline = chunk.indexOf('\n');
    if(newline < 0)
      return;
    chunk.remove(0, newline + 1);
    m_DiscardingTailLine = false;
  }
  m_TailBytes += chunk;
  int lineStart = 0;
  int newline = -1;
  while((newline = m_TailBytes.indexOf('\n', lineStart)) >= 0)
  {
    const QByteArray line = m_TailBytes.mid(lineStart, newline - lineStart).trimmed();
    lineStart = newline + 1;
    if(line.isEmpty() || line.size() > MaxTailEventLineBytes)
      continue;
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(line, &parseError);
    if(parseError.error != QJsonParseError::NoError || !document.isObject())
      continue;
    const QJsonObject event = document.object();
    if(event.value(QLatin1String("taskId")).toString() == m_TailedTaskId)
      m_Store.ConsumeEvent(event);
  }
  if(lineStart > 0)
    m_TailBytes = m_TailBytes.mid(lineStart);
  if(m_TailBytes.size() > MaxTailEventLineBytes)
  {
    m_TailBytes.clear();
    m_DiscardingTailLine = true;
  }
}
