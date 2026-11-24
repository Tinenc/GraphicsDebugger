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

#include <functional>
#include <memory>
#include <QByteArray>
#include <QElapsedTimer>
#include <QFrame>
#include <QHash>
#include <QImage>
#include <QJsonArray>
#include <QMap>
#include <QPair>
#include <QPointer>
#include <QSet>
#include <QStringList>
#include <QVector>
#include "Code/Interface/QRDInterface.h"
#include "Code/ReplayLab/ReplayLabIntegration.h"
#include "AIConversationView.h"

class QComboBox;
class QButtonGroup;
class QCommandLinkButton;
class QLabel;
class QLineEdit;
class QHBoxLayout;
class QMimeData;
class QMenu;
class QPlainTextEdit;
class QProcess;
class QPushButton;
class QScrollArea;
class QTimer;
class QToolButton;
class QVBoxLayout;
class AIConversationListView;
class AIPendingChatView;
class AIAgentMonitorWindow;
class AIAgentTraceStore;
class CaptureContext;
struct AIConfigurationProcessState;

// Native qrenderdoc control surface for ReplayLab's provider-neutral Bread AI host. The
// provider SDKs and managed Agent processes deliberately remain out-of-process so they cannot
// destabilise replay or gain implicit access to ICaptureContext.
class AIAssistant : public QFrame, public ICaptureViewer
{
  Q_DECLARE_TR_FUNCTIONS(AIAssistant)

public:
  explicit AIAssistant(CaptureContext &ctx, QWidget *parent = 0);
  ~AIAssistant();

  QWidget *Widget() { return this; }

  void ReportError(const QString &message);
  void StartCaptureIntent(const QString &prompt,
                          const QString &focusStage = QString(),
                          const QString &intentKind = QString());
  // Adds immutable, untrusted reference text to the composer without sending a request. Returns
  // false when the selection is empty or exceeds the bounded attachment budget.
  bool AddTextAttachment(const QString &title, const QString &text,
                          const QString &source = QString(), bool code = false,
                          const QString &contextJSON = QString());
  // CaptureContext calls this after Save Capture As so the in-memory thread history follows the
  // permanent RDC path without modifying the RDC itself.
  void RebindCaptureFile(const QString &capturePath);
  void Shutdown();

  // Builds the AI page embedded in qrenderdoc's normal Settings dialog. The returned widget is
  // owned by parent. Connection credentials are never written to the profile file or process
  // command line; the user may keep one in this instance or store it through AgentHost in the
  // operating-system credential vault.
  QWidget *CreateSettingsPage(QWidget *parent);

  // ICaptureViewer
  void OnCaptureLoaded() override;
  void OnCaptureClosed() override;
  void OnSelectedEventChanged(uint32_t eventId) override;
  void OnEventChanged(uint32_t eventId) override;

private:
  struct ClarificationOption
  {
    QString id;
    QString label;
    QString description;
  };

  struct ClarificationPrompt
  {
    QString id;
    QString question;
    QString selectionMode;
    QVector<ClarificationOption> options;
    bool allowOther = false;
    QString submitLabel;
    // Preserve the user goal which caused this clarification. The visible reply is often only a
    // choice label (for example "Fragment Shader") and must not replace the original requested
    // outcome when AgentHost reconstructs workflow-effect authority.
    QString originRequest;
    // Long origins are never stored as a misleading prefix. Durable workflows keep the complete
    // request in AgentHost; the native UI persists this UTF-8 SHA-256 for the typed reply.
    QString originRequestSha256;
    // The context which produced this question remains a candidate when the user answers later.
    // The native UI also freezes the then-current state, so AgentHost can resolve the user's
    // intent instead of silently rebinding the follow-up to whichever EID happens to be selected.
    QByteArray originContextSnapshot;
    QString originContextInstanceId;

    bool isValid() const
    {
      return !id.isEmpty() && !question.isEmpty() &&
             (selectionMode == QLatin1String("single") ||
              selectionMode == QLatin1String("multiple")) &&
             options.size() >= 2 && options.size() <= 6;
    }
  };

  struct ClarificationResponse
  {
    QString clarificationId;
    QStringList optionIds;
    QString otherText;
    QString originRequest;
    QString originRequestSha256;
    QByteArray originContextSnapshot;
    QString originContextInstanceId;
    QByteArray currentContextSnapshot;
    QString currentContextInstanceId;

    bool isValid() const
    {
      return !clarificationId.isEmpty() && (!optionIds.isEmpty() || !otherText.isEmpty());
    }
  };

  struct ConversationThread
  {
    QString id;
    QString title;
    // Connection/model selection is conversation-owned. Switching to another conversation
    // changes the composer pickers without changing an already running AgentHost process.
    QString profileId;
    QString modelId;
    qint64 createdMs = 0;
    qint64 updatedMs = 0;
    int omittedMessageCount = 0;
    bool pinned = false;
    bool unread = false;
    QVector<AIChatMessage> messages;
    QSet<QString> incompleteHistoryMessageIds;
    ClarificationPrompt clarification;
  };

  struct PendingChat
  {
    QString taskId;
    QString messageId;
    QString conversationId;
    // Freeze the conversation-owned provider selection at submit time so later picker changes
    // cannot rebind queued work to another provider or model.
    QString profileId;
    QString modelId;
    QString prompt;
    QString effectivePrompt;
    QString skill;
    // MCP servers the message named with "@mcp:<id>"; enabled for this request only.
    QStringList mcpServerIds;
    QString focusStage;
    QString intentKind;
    QVector<AIChatAttachment> attachments;
    QStringList historyExcludeMessageIds;
    QByteArray contextSnapshot;
    QString contextInstanceId;
    // Capture-bound requests remember their exact submit-time binding. Only one bridge freeze is
    // begun at a time; later requests stay local until the preceding result file is terminal.
    QString contextFreezeJobId;
    QString requestedCaptureSessionId;
    qint64 requestedEventId = 0;
    qint64 requestedSelectedEventId = 0;
    qint64 launchBridgeReadinessDeadlineMs = 0;
    bool contextFreezeRequired = false;
    ClarificationResponse clarificationResponse;
    bool includeConversation = true;
  };

  struct ChatExecution
  {
    PendingChat request;
    QProcess *process = NULL;
    QTimer *startTimer = NULL;
    void *jobHandle = NULL;
    void *processInformation = NULL;
    QString contextFile;
    QString cancelFile;
    QByteArray stdoutData;
    QByteArray stderrData;
    QByteArray pendingInput;
    bool promptSent = false;
  };

  struct ComposerRasterAnimation
  {
    QVector<QImage> frames;
    QVector<int> frameDelaysMs;
    QSize sourceSize;

    bool isValid() const
    {
      return frames.size() >= 2 && frameDelaysMs.size() == frames.size() &&
             sourceSize.isValid();
    }
  };

  void appendSystem(const QString &text, bool error = false,
                    const QString &conversationId = QString());
  QString appendUser(const QString &text, const QString &taskId = QString(),
                     const QString &state = QString(),
                     const QString &conversationId = QString(),
                     const QVector<AIChatAttachment> &attachments =
                         QVector<AIChatAttachment>());
  void appendAssistant(const QString &text, const QString &conversationId = QString(),
                       const QString &taskId = QString(), const QJsonArray &artifacts = QJsonArray());
  void renderConversation(bool refreshList = false);
  void scheduleConversationRender();
  void refreshConversationList();
  void refreshPendingChats();
  void createConversation();
  void deleteConversation(const QString &conversationId = QString());
  void forgetDeletedTask(const QString &taskId);
  void toggleConversationPinned(const QString &conversationId);
  void selectConversation(const QString &conversationId);
  ConversationThread *conversation(const QString &conversationId = QString());
  const ConversationThread *conversation(const QString &conversationId = QString()) const;
  AIChatMessage *message(const QString &conversationId, const QString &messageId);
  AIChatMessage *taskMessage(const QString &taskId);
  void addTaskActivity(const QString &taskId, const QString &kind, const QString &title,
                       const QString &detail = QString(), bool completed = false,
                       bool error = false, qint64 timestampMs = 0,
                       const QString &activityId = QString());
  void appendTaskActivityText(const QString &taskId, const QString &kind, const QString &title,
                              const QString &text, bool reset, bool completed, bool error,
                              qint64 timestampMs, const QString &activityId);
  bool updateTaskActivityLifecycle(const QString &taskId, const QString &kind,
                                   const QString &title, bool completed, bool error,
                                   qint64 timestampMs, const QString &activityId);
  void setTaskState(const QString &taskId, const QString &state, bool collapseActivity);
  void updateTaskProgressUI();
  void updateComposerState();
  bool clarificationFromJSON(const QJsonObject &object, const QString &fallbackId,
                             ClarificationPrompt &clarification) const;
  QJsonObject clarificationToJSON(const ClarificationPrompt &clarification) const;
  void bindClarificationOrigin(ClarificationPrompt &clarification, const QString &taskId) const;
  void updateClarificationUI();
  void updateClarificationSubmitState();
  void submitClarification();
  void clearClarification(const QString &conversationId);
  void handleTranscriptLink(const QUrl &url);

  void loadConversations();
  void saveConversations();
  void scheduleConversationSave();
  void resetConversations();
  QJsonObject conversationToJSON(const ConversationThread &thread, int *remainingChars) const;
  bool conversationFromJSON(const QJsonObject &object, ConversationThread &thread) const;

  void updateCaptureStatus();
  void showShaderIntentMenu(QWidget *anchor);
  void clearCaptureTasks();
  void chooseAttachmentFiles();
  void annotateRenderTarget();
  void editRenderTargetAnnotation(const QString &attachmentId);
  bool addRenderTargetAnnotation(const QImage &original, const QImage &annotated,
                                 const QJsonObject &context);
  void requestSystemThumbnail(const QString &attachmentId, const QString &canonicalPath,
                              qint64 expectedBytes, qint64 expectedModifiedUnixMs,
                              bool preferRasterDecoder = false);
  void requestInlineRasterThumbnail(const QString &attachmentId, const QByteArray &encodedBytes);
  bool addAttachmentFiles(const QStringList &paths);
  bool addMimeAttachments(const QMimeData *mimeData);
  bool addComposerAttachment(const AIChatAttachment &attachment, QString *error = NULL);
  void removeComposerAttachment(const QString &attachmentId);
  void previewComposerAttachment(const QString &attachmentId);
  void rebuildAttachmentChips();

  QString findAgentCLI() const;
  QString findBridgeInstance() const;
  bool findBridgeBinding(QString *instanceId, QString *captureSessionId) const;
  QString taskEventRoot() const;
  QString activeConversationTaskId() const;
  void configureProcess(QProcess *process, const QString &credentialProfile,
                        void **processInformation) const;
  typedef std::function<void(int exitCode, const QByteArray &standardOutput,
                             const QString &diagnostic, bool commandUnavailable)>
      CLICompletion;
  void runCLIAsync(const QStringList &arguments, const QByteArray &input, int timeoutMs,
                   QObject *owner, const CLICompletion &completion,
                   const QString &credentialProfile = QString(),
                   const std::function<void(const QByteArray &)> &outputProgress = {});
  void runConnectionCheck(QWidget *parent, const QString &profile, bool executionTest,
                          const std::function<void()> &completion = {});

  void refreshProfiles(QObject *completionOwner = NULL,
                       const std::function<void()> &completion = std::function<void()>());
  void refreshModelCombo();
  bool repairConversationConnectionSelections();
  void syncConnectionSelectionFromConversation();
  void selectProfile(int index);
  void selectModel(int index);
  bool connectionMutationAllowed(QWidget *parent) const;
  void manageConnections();
  void runDoctor();
  // Settings > AI: the user's Skill folders, per-Skill enablement and external MCP
  // servers. Both engines read the same configuration through the AgentHost.
  QWidget *createToolCatalogSection(QWidget *parent, QWidget *connectionsPage);
  // Settings > AI edits the same three files the Agent configures itself: the Skill
  // catalog, the MCP server list and the asset-reader registry.
  void showSkillDocument(QWidget *parent, const QString &skillId,
                         const std::function<void()> &reload);
  void presentSkillDocument(QWidget *parent, const QString &skillId, const QJsonObject &document,
                            const std::function<void()> &reload);
  enum class ToolDialogMode
  {
    Any,
    Server,
    Program
  };
  void editToolProvider(QWidget *parent, const QJsonObject &existing,
                        ToolDialogMode mode = ToolDialogMode::Any);
  void editBuiltinUnpacker(QWidget *parent, const QJsonObject &unpacker,
                           const QString &detectedProgram = QString());
  QJsonArray builtinUnpackerCatalog() const { return m_BuiltinUnpackerCatalog; }
  QJsonArray m_BuiltinUnpackerCatalog;
  void detectAssetReaders(QWidget *parent);
  // Row text for the tool list: how it is reached, what it may be used for, and
  // whether its interface has actually been proved.
  static QString connectionSummary(const QJsonObject &connection);
  static QString connectionDetail(const QJsonObject &connection);
  static QString toolRoleSummary(const QJsonObject &tool);
  // The conversational route to the same settings: the request goes into the composer
  // so the person sees exactly what is being asked before it is sent.
  void askAIToConfigure(QWidget *parent);
  void askAIToCompleteUnpacker(QWidget *parent, const QJsonObject &tool);
  void presentDetectedAssetReaders(QWidget *parent, const QJsonArray &candidates);
  // Runs one configuration edit and refreshes the panel; reports a refusal to the user
  // rather than leaving the list silently unchanged.
  void runConfigurationEdit(QWidget *parent, const QStringList &arguments,
                            const QByteArray &input);
  void refreshToolMentionCatalog();
  QStringList mentionedSkillIds(const QString &prompt) const;
  std::function<void()> m_ToolCatalogReload;
  QStringList mentionedMcpServerIds(const QString &prompt) const;

  void refreshConnectionStatus();
  void cancelConfigurationProcesses();
  void forceStopProcessesForDestruction();
  bool attachKillOnCloseJob(QProcess *process, void *&jobHandle, QString *error) const;
  bool resumeSuspendedProcess(void *processInformation, QString *error) const;
  void closeKillOnCloseJob(void *&jobHandle) const;

  QString newTaskId() const;
  QString newMessageId() const;
  QString conversationPrompt(const QString &prompt, const QString &conversationId,
                             const QStringList &excludeMessageIds = QStringList(),
                             const ClarificationResponse &clarificationResponse =
                                 ClarificationResponse(),
                             const QVector<AIChatAttachment> &attachments =
                                 QVector<AIChatAttachment>(),
                             const QString &profileId = QString()) const;
  void startChat(const QString &prompt, bool includeConversation = true,
                 const QString &focusStage = QString(),
                 const ClarificationResponse &clarificationResponse = ClarificationResponse(),
                 const QString &intentKind = QString(),
                 const QVector<AIChatAttachment> &attachments =
                     QVector<AIChatAttachment>());
  void advanceChatContextFreeze();
  void updateChatContextFreezeTimer();
  void pollChatContextFreeze();
  void failQueuedChatContextFreeze(const QString &taskId, const QString &jobId,
                                   const QString &diagnostic);
  void discardChatContextFreeze(PendingChat &request);
  void discardAllChatContextFreezes();
  ChatExecution *chatExecution(const QString &taskId) const;
  ChatExecution *chatExecution(QProcess *process) const;
  ChatExecution *conversationExecution(const QString &conversationId) const;
  void clearChatContextFile(ChatExecution *execution);
  void requestChatCancellation(ChatExecution *execution);
  void clearChatCancelFile(ChatExecution *execution);
  void launchChat(PendingChat request);
  void handleChatProcessStarted(QProcess *process);
  void failChatProcessStart(QProcess *process, const QString &diagnostic);
  void startNextChat();
  void adjustQueuedChat(const QString &taskId);
  void editQueuedChat(const QString &taskId);
  void deleteQueuedChat(const QString &taskId);
  void cancelChat(const QString &terminalState = QString(), bool continueQueue = true,
                  const QString &taskId = QString(), int cooperativeWaitMs = 0,
                  bool refreshUI = true);
  void finishChatProcess(const QString &taskId, int exitCode);
  void drainChatTaskEvents(const QString &taskId, int exitCode, qint64 remainingBytes);
  void finishChatResult(const QString &taskId, int exitCode);

  void watchTask(const QString &taskId, const QString &userPrompt = QString(),
                 const QString &conversationId = QString(),
                 const QString &messageId = QString());
  void unwatchTask(const QString &taskId);
  void updateEventTimer();
  void pollTaskEvents();
  qint64 pollTaskEvent(const QString &taskId, qint64 maxReadBytes, bool *atEnd = NULL);
  void consumePublicAgentMessageEvent(const QString &taskId, const QJsonObject &event,
                                      const QJsonObject &eventData, qint64 timestampMs);
  void consumeTaskEvent(const QString &taskId, const QByteArray &line);
  void finishTask(const QString &taskId, const QString &kind, const QString &summary,
                  qint64 timestampMs = 0);

  CaptureContext &m_Ctx;

  QLabel *m_Status = NULL;
  QLabel *m_Context = NULL;
  QLabel *m_QueueStatus = NULL;
  AIConversationListView *m_ConversationList = NULL;
  AIPendingChatView *m_PendingChats = NULL;
  QComboBox *m_ProfileCombo = NULL;
  QComboBox *m_ModelCombo = NULL;
  AIConversationView *m_Transcript = NULL;
  QFrame *m_ClarificationPanel = NULL;
  QLabel *m_ClarificationQuestion = NULL;
  QWidget *m_ClarificationOptions = NULL;
  QVBoxLayout *m_ClarificationOptionsLayout = NULL;
  QButtonGroup *m_ClarificationButtonGroup = NULL;
  QLineEdit *m_ClarificationOtherEdit = NULL;
  QPushButton *m_ClarificationSubmit = NULL;
  QVector<QCommandLinkButton *> m_ClarificationButtons;
  QString m_VisibleClarificationKey;
  QScrollArea *m_AttachmentScroll = NULL;
  QWidget *m_AttachmentStrip = NULL;
  QHBoxLayout *m_AttachmentLayout = NULL;
  QPushButton *m_AddAttachment = NULL;
  QPushButton *m_AnnotateRT = NULL;
  QPlainTextEdit *m_Prompt = NULL;
  QToolButton *m_TaskProgress = NULL;
  QMenu *m_TaskProgressMenu = NULL;
  QPushButton *m_Send = NULL;
  QPushButton *m_Cancel = NULL;
  AIAgentTraceStore *m_AgentTraceStore = NULL;
  AIAgentMonitorWindow *m_AgentMonitor = NULL;

  QTimer *m_EventTimer = NULL;
  QTimer *m_ConnectionStatusTimer = NULL;
  QTimer *m_ConversationSaveTimer = NULL;
  QTimer *m_ConversationRenderTimer = NULL;
  QTimer *m_ChatContextFreezeTimer = NULL;
  QString m_CLIPath;
  QString m_InstanceId;
  QString m_ActiveProfile;
  QString m_ActiveKeyEnv;
  QString m_ActiveModel;
  QString m_ActiveConversationId;
  QString m_CaptureConversationPath;
  QString m_ChatContextFreezeTaskId;
  QString m_ChatContextFreezeJobId;
  QString m_ChatContextFreezeResultPath;
  QString m_ChatContextFreezeCaptureSessionId;
  QString m_ChatContextFreezeInstanceId;
  // A bridge heartbeat can briefly disappear while project restoration owns replay. Keep the
  // accepted capture-bound request queued until the same binding returns or this wait expires.
  QString m_ChatBridgeRetryTaskId;
  qint64 m_ChatBridgeRetryDeadlineMs = 0;
  bool m_ChatBridgeRetryScheduled = false;
  bool m_ChatLaunchRetryScheduled = false;
  qint64 m_ChatContextFreezeEventId = 0;
  qint64 m_ChatContextFreezeSelectedEventId = 0;
  qint64 m_ChatContextFreezeDeadlineMs = 0;
  bool m_ChatContextFreezeTimedOut = false;
  bool m_ProfileComboLoading = false;
  bool m_ProfileRefreshRunning = false;
  bool m_ProfileRefreshPending = false;
  quint64 m_ProfileRefreshGeneration = 0;
  struct ProfileRefreshCompletion
  {
    QPointer<QObject> owner;
    std::function<void()> function;
  };
  QVector<ProfileRefreshCompletion> m_ProfileRefreshCompletions;
  bool m_ConnectionDialogOpen = false;
  bool m_ShuttingDown = false;

  // Indexed by stable connection id, not environment-variable name. This holds only explicit
  // session credentials; durable credentials remain in the OS vault and never enter this map.
  QMap<QString, QString> m_SessionSecrets;
  QHash<QString, QString> m_ProfileKeyEnvs;
  QHash<QString, QStringList> m_ProfileModels;
  QHash<QString, QString> m_ProfileDefaultModels;
  QHash<QString, QStringList> m_ProfileRunInputSchemas;
  QJsonArray m_ProfileRows;
  // Settings pages can outlive this panel. Keep every configuration command registered with the
  // assistant as well as its UI owner so Shutdown can terminate the contained process tree and
  // suppress callbacks which captured panel state without waiting on the GUI thread.
  QHash<QProcess *, std::shared_ptr<AIConfigurationProcessState>> m_ConfigurationProcesses;
  QSet<QString> m_KnownTasks;
  QSet<QString> m_RunningTasks;
  QSet<QString> m_CompletedTasks;
  QSet<QString> m_DeletedTasks;
  int m_TaskEventPollCursor = 0;
  int m_TaskEventRecordsRemaining = 0;
  bool m_TaskEventBudgetActive = false;
  QElapsedTimer m_TaskEventPollClock;
  QHash<QString, qint64> m_TaskOffsets;
  QHash<QString, qint64> m_TaskSequences;
  QHash<QString, QByteArray> m_TaskTails;
  QHash<QString, QString> m_TaskTexts;
  // Characters of live raw reasoning already shown per task (bounded in the conversation).
  // Ids offered by the composer's "@" picker, refreshed from the AgentHost.
  QStringList m_MentionSkillIds;
  QStringList m_MentionMcpIds;
  struct LiveAgentMessageStream
  {
    QString activityId;
    QString activityKind;
    // The Agent Monitor owns the exact provider payload. The ordinary conversation keeps it only
    // while this stream is active so structured JSON can be projected incrementally into prose.
    QString sourceText;
    QString projectedText;
    QString replacementId;
    QString replacementText;
    int replacementChunks = 0;
    int replacementBytes = -1;
    int receivedChars = 0;
    bool activityCreated = false;
    bool omitted = false;
    // When this stream last received a delta. The producer mints a new item id per
    // 8 KB chunk and never marks a child message's lifecycle terminal, so the map
    // fills up on a long run; this is what decides which one to evict.
    qint64 touchedMs = 0;
  };
  QHash<QString, QHash<QString, LiveAgentMessageStream>> m_TaskAgentMessageStreams;
  QHash<QString, QString> m_UserPrompts;
  QHash<QString, QString> m_TaskConversationIds;
  QHash<QString, QString> m_TaskMessageIds;
  QVector<AIChatAttachment> m_ComposerAttachments;
  // Ephemeral previews created while a user-selected local file path is available. They are never
  // serialized, copied into the conversation wire format, or used to reopen historical files.
  QHash<QString, QImage> m_ComposerSystemThumbnails;
  QHash<QString, ComposerRasterAnimation> m_ComposerSystemAnimations;
  QVector<ConversationThread> m_Conversations;
  QVector<PendingChat> m_ChatQueue;
  QHash<QString, ChatExecution *> m_ChatExecutions;
  bool m_LoadingConversations = false;
  // Set when this capture's conversation store could not be read. The store is then
  // never written, so a transient read failure cannot destroy the stored history.
  bool m_ConversationStoreReadOnly = false;
};
