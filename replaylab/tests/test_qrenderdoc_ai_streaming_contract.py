from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AI_ROOT = REPOSITORY_ROOT / "qrenderdoc" / "Windows" / "AI"


def _source(name: str) -> str:
    return (AI_ROOT / name).read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


def test_terminal_answer_uses_model_text_without_a_report_gate() -> None:
    source = _source("AIAssistant.cpp")
    header = _source("AIAssistant.h")
    terminal = _section(
        source,
        "void AIAssistant::finishTask(",
        "void AIAssistant::OnCaptureLoaded()",
    )

    assert "renderTaskReport" not in source + header
    assert "const QJsonObject &report" not in header
    assert "const QJsonObject &verification" not in header
    assert 'eventData.value(QStringLiteral("report")).toObject()' not in source
    assert 'resultObject.value(QStringLiteral("verification")).toObject()' not in source
    assert "finishTask(taskId, terminalStatus, summary, eventTimestampMs);" in source
    assert "if(exitCode == 0 && validTerminal)" in source
    assert "finishTask(taskId, terminalStatus, result);" in source
    assert "answerTerminal && !accumulated.isEmpty() ? accumulated : summary" in terminal
    assert "appendAssistant(result.isEmpty() ? fallback : result, conversationId, taskId);" in terminal
    assert "addTaskActivity(taskId, kind, title" in terminal


def test_conversation_stream_updates_are_coalesced_before_rich_text_layout() -> None:
    source = _source("AIAssistant.cpp")
    activity = _section(
        source,
        "void AIAssistant::addTaskActivity(",
        "void AIAssistant::setTaskState(",
    )
    state = _section(
        source,
        "void AIAssistant::setTaskState(",
        "bool AIAssistant::clarificationFromJSON(",
    )
    message_delta = _section(
        source,
        'else if(kind == QLatin1String("message-delta") ||',
        'else if(kind == lit("reasoning-delta"))',
    )

    for hot_path in (activity, state, message_delta):
        assert "scheduleConversationRender();" in hot_path
        assert "renderConversation();" not in hot_path

    # The live answer must have one authoritative QString. Mirroring the accumulated value into
    # provisionalText on every delta makes the next append detach and copy the whole answer.
    assert "QString &answer = item ? item->provisionalText : m_TaskTexts[taskId];" in message_delta
    assert "item->provisionalText = answer;" not in message_delta

    scheduler = _section(
        source,
        "void AIAssistant::scheduleConversationRender()",
        "void AIAssistant::refreshConversationList()",
    )
    assert "contentChars < 64 * 1024" in scheduler
    assert "contentChars < 512 * 1024" in scheduler
    assert ": 750;" in scheduler

    activity_append = _section(
        source,
        "void AIAssistant::addTaskActivity(",
        "void AIAssistant::setTaskState(",
    )
    assert "MaxLiveConversationActivities = 512" in source
    assert "LiveConversationActivityTrimTarget = 384" in source
    assert "item->activity.size() > MaxLiveConversationActivities" in activity_append


def test_jsonl_pollers_scan_chunks_without_repeated_front_removal() -> None:
    assistant = _source("AIAssistant.cpp")
    assistant_poll = _section(
        assistant,
        "qint64 AIAssistant::pollTaskEvent(",
        "void AIAssistant::consumeTaskEvent(",
    )
    assert "pending.indexOf('\\n', lineStart)" in assistant_poll
    assert "pending.remove(0" not in assistant_poll
    drain = _section(
        assistant,
        "void AIAssistant::drainChatTaskEvents(",
        "void AIAssistant::finishChatResult(",
    )
    # Exiting agents can leave a chunk of tiny events. Byte-bounding it alone still allows all
    # those events to monopolize the GUI; the live record/time budget must cover the exit drain.
    assert drain.index("m_TaskEventBudgetActive = true;") < drain.index("pollTaskEvent(")
    assert drain.index("pollTaskEvent(") < drain.index("m_TaskEventBudgetActive = false;")
    # Slow disk I/O must not produce a zero-progress drain that abandons buffered final events.
    assert "lineStart > 0 && m_TaskEventPollClock.elapsed()" in assistant_poll

    monitor = _source("AIAgentMonitorWindow.cpp")
    monitor_poll = monitor[monitor.index("void AIAgentMonitorWindow::PollTaskFile()") :]
    assert "m_TailBytes.indexOf('\\n', lineStart)" in monitor_poll
    assert "m_TailBytes.remove(0" not in monitor_poll
    assert "MaxTailReadBytes = 128 * 1024" in monitor


def test_trace_buffers_keep_trim_headroom_and_skip_per_token_cache_metadata() -> None:
    source = _source("AIAgentTraceStore.cpp")
    assert "TaskEventLogTrimTargetChars = 6 * 1024 * 1024" in source
    assert "task.eventLog.size() - TaskEventLogTrimTargetChars" in source
    assert "const int trimTarget = maximumChars * 3 / 4" in source
    assert "const QString cache = isStreamOnly ? QString()" in source
    assert "AgentRecordTrimTarget = 1536" in source
    assert "agent.records.size() - AgentRecordTrimTarget" in source
    assert "agent.records.size() - MaxAgentRecords" not in source


def test_monitor_text_updates_stay_incremental_and_render_only_the_visible_tab() -> None:
    source = _source("AIAgentMonitorWindow.cpp")
    updater = _section(
        source,
        "void updateStreamingTextView(",
        "}    // namespace",
    )
    assert "renderedRevision == revision && renderedChars <= text.size()" in updater
    assert updater.count("edit->setPlainText(text);") == 1
    assert "if(visibleTrace == m_TaskLog)" in source
    assert "if(visibleTrace == m_RawReasoning)" in source
    assert "m_LastStructureRefreshMs" in source


def test_monitor_bounds_materialized_task_log_and_pauses_hidden_tail() -> None:
    source = _source("AIAgentMonitorWindow.cpp")
    task_log = _section(
        source,
        "QString materializedTaskLog(",
        "}    // namespace",
    )
    constructor = _section(
        source,
        "AIAgentMonitorWindow::AIAgentMonitorWindow(",
        "AIAgentMonitorWindow::~AIAgentMonitorWindow()",
    )
    open_task = _section(
        source,
        "bool AIAgentMonitorWindow::OpenTask(",
        "bool AIAgentMonitorWindow::ReplayTaskFile(",
    )
    hide_all = _section(
        source,
        "void AIAgentMonitorWindow::HideAll()",
        "void AIAgentMonitorWindow::SetTaskEventRoot(",
    )
    poll = source[source.index("void AIAgentMonitorWindow::PollTaskFile()") :]

    assert "MaxMaterializedTaskLogChars = 1024 * 1024" in source
    assert "TaskLogTrimTargetChars = 768 * 1024" in source
    assert 'translate("AIAgentTraceStore", "[Older task log events omitted]")' in task_log
    assert "materializedTaskLog(text, TaskLogTrimTargetChars)" in task_log
    assert "m_TaskTailTimer->start();" not in constructor
    assert "if(!isVisible() || m_TailedTaskId.isEmpty()" in poll
    assert "m_TaskTailTimer->stop();" in source
    assert "m_TaskTailTimer->start();" in source
    assert "m_TailedTaskId != taskId || m_TailedFilePath != absoluteFilePath" in open_task
    assert "StopTailingTask();" not in hide_all


def test_conversation_scroll_uses_persistent_follow_tail_state() -> None:
    header = _source("AIConversationView.h")
    source = _source("AIConversationView.cpp")
    render = source[source.index("void AIConversationView::render(bool preserveScroll)") :]
    constructor = _section(
        source,
        "AIConversationView::AIConversationView(QWidget *parent)",
        "void AIConversationView::setMessages(",
    )

    assert "bool m_FollowTail = true" in header
    assert "ViewportAnchor m_ViewportAnchor" in header
    assert '"Jump to latest message"' in source
    assert "&QAbstractSlider::actionTriggered" in constructor
    value_changed = [line for line in constructor.splitlines() if "&QScrollBar::valueChanged" in line]
    assert all("updateVisualReplies();" in line and "detachFromTail" not in line for line in value_changed)
    assert "detachFromTail();" in constructor
    assert "m_UserInteractionGeneration" in header
    assert "wheel->pixelDelta().y()" in source
    assert "userInteractionGeneration == m_UserInteractionGeneration" in source
    assert "cursorForPosition(QPoint(anchorX, 1))" in source
    assert "m_Browser->cursorRect(cursor).top()" in source
    assert "scheduleViewportAnchorRestore();" in constructor
    assert "document->setHtml(inlineTranscriptStyles(html));" in render
    assert render.index("m_AdjustingScroll = true;") < render.index("m_Browser->setDocument(document);")
    assert render.index("m_Browser->setDocument(document);") < render.index("m_AdjustingScroll = false;")
    assert "if(m_FollowTail)" in render
    assert "scrollToLatest();" in render
    assert "restoreViewportAnchor();" in render


def test_conversation_text_supports_keyboard_selection_and_copy() -> None:
    view = _source("AIConversationView.cpp")
    assistant = _source("AIAssistant.cpp")
    constructor = _section(
        view,
        "AIConversationView::AIConversationView(QWidget *parent)",
        "void AIConversationView::setMessages(",
    )
    event_filter = _section(
        view,
        "bool AIConversationView::eventFilter(QObject *watched, QEvent *event)",
        "void AIConversationView::activateLink(",
    )

    assert "Qt::TextBrowserInteraction |\n                                     Qt::TextSelectableByKeyboard" in constructor
    assert "m_Browser->setFocusPolicy(Qt::StrongFocus);" in constructor
    assert "key->matches(QKeySequence::Copy)" in event_filter
    assert "m_Browser->copy();" in event_filter

    for label in ("m_Status", "m_Context", "m_ClarificationQuestion"):
        begin = assistant.index(
            f"{label}->setTextInteractionFlags(Qt::TextSelectableByMouse |"
        )
        selectable_label = assistant[begin : begin + 260]
        assert "Qt::TextSelectableByKeyboard" in selectable_label
        assert f"{label}->setFocusPolicy(Qt::StrongFocus);" in selectable_label


def test_accepted_send_releases_old_transcript_selection_before_async_launch() -> None:
    source = _source("AIAssistant.cpp")
    submit = _section(source, "void AIAssistant::startChat(", "void AIAssistant::startNextChat()")
    assert submit.index("m_ChatQueue.push_back(request);") < submit.index("followSubmittedTurn();")
    assert submit.index("followSubmittedTurn();") < submit.index("advanceChatContextFreeze();")
    assert "request.conversationId == m_ActiveConversationId" in submit
    # Background events, queued launches and conversation switches cannot override a later
    # deliberate scroll back into history. The explicit accepted-send path owns this intent.
    assert source.count("m_Transcript->followSubmittedTurn();") == 1


def test_live_conversation_appends_without_refreshing_the_document_or_selection() -> None:
    header = _source("AIConversationView.h")
    source = _source("AIConversationView.cpp")
    constructor = _section(
        source,
        "AIConversationView::AIConversationView(QWidget *parent)",
        "void AIConversationView::setMessages(",
    )
    setter = _section(
        source,
        "void AIConversationView::setMessages(",
        "void AIConversationView::clear()",
    )
    jump_to_tail = _section(
        source,
        "void AIConversationView::scrollToLatest()",
        "void AIConversationView::updateTailControl()",
    )

    elapsed_tick = _section(
        source,
        "void AIConversationView::advanceElapsedText()",
        "void AIConversationView::setMessages(",
    )
    elapsed_rebuild = _section(
        source,
        "void AIConversationView::rebuildElapsedCursors()",
        "void AIConversationView::advanceElapsedText()",
    )
    assert "m_ElapsedTimer" in header
    assert "render(" not in elapsed_tick
    assert "setHtml(" not in elapsed_tick
    # Qt anchors mark only their first character. Cover the whole translated
    # duration label while painting the timer without changing the document.
    assert "target.text = anchor.value().second;" in elapsed_rebuild
    assert "target.length = target.text.size();" in elapsed_rebuild
    assert "span.selectedText() != target.text" in elapsed_rebuild
    assert "fragment.length()" not in elapsed_rebuild
    assert "edit.setPosition(position + entry.length" in elapsed_tick
    assert "overlay.length = entry.length;" in elapsed_tick
    assert "setElapsedOverlays(overlays)" in elapsed_tick
    assert "insertText(" not in elapsed_tick
    assert "canKeepTranscriptDocument(m_Messages, messages)" in setter
    assert "if(keepDocument && !flushSelectionRender)" in setter
    keep_document = setter[setter.index("if(keepDocument && !flushSelectionRender)") :]
    keep_document = keep_document[: keep_document.index("bool hasLive = false;")]
    assert "updateLiveTextTimer();" in keep_document
    assert "render();" not in keep_document
    assert "&QTextBrowser::selectionChanged" in constructor
    assert "m_SelectingText = true;" in source
    assert "m_RenderPendingForSelection = true;" in setter
    assert "m_LiveTextTimer->stop();" in setter
    assert "flushDeferredRender();" in source
    assert "bool hadLive = false;" in setter
    assert "bool hasLive = false;" in setter
    assert (
        "if(!conversationChanged && !m_FollowTail && !hadLive && !hasLive)" in setter
    )
    assert "m_RenderPendingWhileDetached = true;" in setter
    assert "return;" in setter
    assert "if(m_RenderPendingWhileDetached)" in jump_to_tail
    assert "render();" in jump_to_tail


def test_child_agent_messages_project_only_schema_qualified_narrative_text() -> None:
    header = _source("AIAssistant.h")
    source = _source("AIAssistant.cpp")
    projection = _section(
        source,
        "bool publicAgentNarrativePath(",
        "struct AIProviderCatalogState",
    )
    public_stream = _section(
        source,
        "void AIAssistant::consumePublicAgentMessageEvent(",
        "void AIAssistant::consumeTaskEvent(",
    )
    consume = _section(
        source,
        "void AIAssistant::consumeTaskEvent(",
        "void AIAssistant::finishTask(",
    )

    assert 'kind == QLatin1String("message-delta") ||' in consume
    assert 'kind == QLatin1String("agent-message-delta")' in consume
    assert "consumePublicAgentMessageEvent(" in consume
    assert "appendTaskActivityText(" in public_stream
    assert "provisionalText" not in public_stream
    assert "publicAgentMessageProjection(" in public_stream
    assert "commentary || !structuredObservation" in public_stream
    assert 'role != QLatin1String("chat")' in public_stream
    assert "publicAgentMessageProjection(stream.sourceText, NULL, true)" in public_stream
    assert "QJsonDocument::fromJson" not in public_stream
    assert ".trimmed()" not in public_stream
    assert 'lit("goal/objective")' in projection
    assert 'lit("goal/constraints/*")' in projection
    assert 'lit("goal/success_criteria/*")' in projection
    assert 'lit("plan/request_summary")' in projection
    assert 'lit("plan/tasks/*/summary")' in projection
    assert 'lit("selectionBasis")' not in projection
    assert 'lit("selection_basis")' not in projection
    assert 'lit("message")' not in projection
    assert 'lit("text")' not in projection
    assert "custom_agent_id" not in projection
    assert "prerequisite_result_sha256s" not in projection
    assert "PublicAgentJsonNarrativeProjector" in projection
    assert "if(expectStructured)" in projection
    assert "source.indexOf(QLatin1Char('{'), start)" in projection
    assert "QJsonDocument::fromJson" not in projection
    assert ".left(4000)" not in projection
    assert "m_Output += text;" in projection
    assert "MaxLiveAgentMessageStreams = 128" in source
    assert "MaxActivityIdChars = 512" in source
    assert "MaxLiveAgentMessageChars = 1024 * 1024" in source
    assert "QHash<QString, QHash<QString, LiveAgentMessageStream>>" in header
    assert "QString sourceText" in header
    assert "QString projectedText" in header
    assert "int receivedChars = 0" in header
    assert 'eventData.value(QLatin1String("reset")).toBool()' in public_stream
    assert 'eventData.value(QLatin1String("replacementId"))' in public_stream
    assert 'eventData.value(QLatin1String("replacementChunkIndex"))' in public_stream
    assert 'eventData.value(QLatin1String("replacementBytes"))' in public_stream
    assert 'eventData.value(QLatin1String("replacementComplete"))' in public_stream
    assert "QCryptographicHash::Sha256" in public_stream
    assert "replacement != stream.sourceText" in public_stream
    # The exact Messages delta, not the earlier sanitized cumulative running snapshot, owns
    # commentary text. Its first delta resets that provisional row and then appends verbatim.
    assert 'messagePhase == QLatin1String("commentary")' in public_stream
    assert 'commentary ? QLatin1String("progressNote")' in public_stream
    assert "(identity + QLatin1Char(':') + itemId).left(512)" in public_stream
    assert "visibleReset |= commentary && !stream.activityCreated;" in public_stream
    assert "stream.activityId = activityId" in public_stream
    assert "stream.activityKind = activityKind" in public_stream
    assert "streams.remove(streamKey)" in public_stream
    assert "sanitized cumulative running snapshot" in consume
    assert "updateTaskActivityLifecycle(" in consume
    assert "activity.exactText" in consume
    assert "completedStreamKeys" in consume

    append = _section(
        source,
        "void AIAssistant::appendTaskActivityText(",
        "bool AIAssistant::updateTaskActivityLifecycle(",
    )
    assert "(!candidate.completed || reset || completed)" in append
    assert "activity->exactText = true" in append

    terminal = _section(
        source,
        "void AIAssistant::setTaskState(",
        "bool AIAssistant::clarificationFromJSON(",
    )
    assert "collapseActivity && activity.liveText && !activity.completed" in terminal
    assert "activity.finishedTimestampMs = terminalTimestampMs" in terminal


def test_live_activity_text_coalesces_received_suffix_without_full_document_rebuild() -> None:
    header = _source("AIConversationView.h")
    source = _source("AIConversationView.cpp")
    reveal = _section(
        source,
        "void AIConversationView::advanceLiveText()",
        "void AIConversationView::rebuildLiveTextCursors()",
    )
    renderer = _section(
        source,
        "QString renderLiveActivityDetail(",
        "QString renderActivityGroup(",
    )

    assert "bool liveText = false" in header
    assert "bool exactText = false" in header
    assert "quint64 liveTextRevision = 0" in header
    assert "int visibleDetailChars = -1" in header
    # Do not synthesize another per-character stream after the provider has already streamed it:
    # large snapshots must catch up in one display frame, including Unicode and complete code.
    assert "LiveTextRevealIntervalMs = 33" in source
    assert "QTextBoundaryFinder" not in source
    assert "const QString suffix = activity.detail.mid(activity.visibleDetailChars);" in reveal
    assert "cursor.value().cursor.insertText(suffix" in reveal
    assert "activity.visibleDetailChars = -1;" in reveal
    assert "setHtml(" not in reveal
    assert "visible.toHtmlEscaped()" in renderer
    assert "safeMarkdown(" not in renderer
    assert '<span class=\\"activity-live-tail\\" id=\\"' in renderer
    assert ".activity-live-tail{font-size" not in source
    assert ".activity-live-tail{color:" in source
    cursor_rebuild = _section(
        source,
        "void AIConversationView::rebuildLiveTextCursors()",
        "void AIConversationView::scrollToMessage(",
    )
    assert "target.format.setAnchor(false);" in cursor_rebuild
    assert "target.format.setAnchorHref(QString());" in cursor_rebuild
    assert "target.format.setAnchorNames(QStringList());" in cursor_rebuild
    assert "palette().brush(QPalette::Disabled, QPalette::Text)" in cursor_rebuild
    assert "target.format.setFontUnderline(false);" in cursor_rebuild


def test_live_activity_text_is_not_silently_line_or_history_truncated() -> None:
    source = _source("AIAssistant.cpp")
    view = _source("AIConversationView.cpp")
    activity_detail = _section(
        view,
        "QString renderActivityDetail(",
        "QString renderLiveActivityDetail(",
    )
    persistence = _section(
        source,
        "QJsonObject AIAssistant::conversationToJSON(",
        "bool AIAssistant::conversationFromJSON(",
    )

    assert "const int maxLines = 10" in activity_detail  # bounded tool/code surface only
    assert "const int maxLines = 7" not in activity_detail
    assert "const int maxLineCharacters = 360" not in activity_detail
    assert "visibleLines.reserve(lines.size())" in activity_detail
    assert "MaxStoredConversationActivities = 256" in source
    assert "isLiveTextActivityKind(activity.kind) ? MaxLiveAgentMessageChars : 4000" in persistence


def test_persisted_conversation_restart_preserves_omission_lineage() -> None:
    header = _source("AIAssistant.h")
    source = _source("AIAssistant.cpp")
    writer = _section(
        source,
        "QJsonObject AIAssistant::conversationToJSON(",
        "bool AIAssistant::conversationFromJSON(",
    )
    reader = _section(
        source,
        "bool AIAssistant::conversationFromJSON(",
        "void AIAssistant::loadConversations()",
    )
    prompt = _section(
        source,
        "QString AIAssistant::conversationPrompt(",
        "void AIAssistant::advanceChatContextFreeze()",
    )

    # A restart must retain the count of whole rows evicted by the 200-message/store bounds.
    assert "int omittedMessageCount = 0" in header
    assert 'object[lit("omittedMessageCount")]' in writer
    assert "eligibleMessageCount - selectedMessages.size()" in writer
    assert 'object.value(lit("omittedMessageCount"))' in reader

    # A prefix persisted only partially is display-only; it and every older row become one
    # unavailable prefix rather than being replayed to AgentHost as fabricated complete history.
    assert "QSet<QString> incompleteHistoryMessageIds" in header
    assert 'row[lit("textTruncated")] = item.text.size() > textLimit' in writer
    assert 'row[lit("historyIncomplete")] = historyIncomplete' in writer
    assert "thread.incompleteHistoryMessageIds.insert(item.id)" in reader
    assert "incompleteBoundary" in prompt
    assert "history.remove(0, incompleteBoundary + 1)" in prompt

    # Native prefix loss and the 16 MiB wire suffix loss must be accumulated, never reset to the
    # size/index of the currently loaded in-memory transcript after restart.
    assert "omittedHistoryCount + history.size()" in prompt
    assert "omittedHistoryCount + i" in prompt
    assert "omittedHistoryCount + i + 1" in prompt

    # A separately installed legacy AgentHost has no typed omittedMessageCount field. Its plain
    # prompt must still disclose the exact unavailable prefix instead of presenting a suffix as
    # complete history.
    assert "auto legacyPrefix = [](int omitted)" in prompt
    assert "HISTORY GAP: %1 older conversation message record(s)" in prompt
    assert "legacyPrefix(qMin(1000000, omittedHistoryCount + i))" in prompt


def test_capture_conversation_does_not_force_an_old_skill_or_stage() -> None:
    source = _source("AIAssistant.cpp")
    header = _source("AIAssistant.h")
    start = _section(
        source,
        "void AIAssistant::startChat(",
        "void AIAssistant::startNextChat()",
    )
    assert "continuationSkill" not in header
    assert "continuationFocusStage" not in header
    assert "submittedContinuationSkill" not in start
    assert "submittedContinuationFocusStage" not in start
    assert "request.focusStage = focusStage;" in start
    assert "request.skill = mentioned.first();" in start
    assert "request.intentKind = intentKind;" in start


def test_every_chat_launch_passes_conversation_identity_and_capture_history() -> None:
    source = _source("AIAssistant.cpp")
    capture_intent = _section(
        source,
        "void AIAssistant::StartCaptureIntent(",
        "void AIAssistant::startChat(",
    )
    launch = _section(
        source,
        "void AIAssistant::launchChat(",
        "void AIAssistant::handleChatProcessStarted(",
    )

    assert (
        "startChat(prompt, false, focusStage, ClarificationResponse(), intentKind);"
        in capture_intent
    )
    assert "createConversation();" in capture_intent
    assert 'lit("--conversation-id")' in launch
    assert "request.conversationId" in launch
    assert "request.includeConversation || !request.attachments.isEmpty()" in launch
    assert ": request.effectivePrompt;" in launch


def test_chat_without_capture_bridge_still_reaches_agent() -> None:
    source = _source("AIAssistant.cpp")
    start = _section(
        source,
        "void AIAssistant::startChat(",
        "void AIAssistant::startNextChat()",
    )
    launch = _section(
        source,
        "void AIAssistant::launchChat(",
        "void AIAssistant::handleChatProcessStarted(",
    )

    # Capture actions need an open RDC at submission. Ordinary conversation
    # and explicitly mentioned general Skills do not.
    assert "if(!intentKind.isEmpty() && !m_Ctx.IsCaptureLoaded())" in start
    assert "if(findBridgeBinding(&request.contextInstanceId, &request.requestedCaptureSessionId))" in start
    assert "request.contextFreezeRequired = true;" in start
    assert 'appendSystem(tr("The capture bridge is not ready yet."), true);' not in start
    assert "else if(request.contextFreezeRequired)" in launch
    assert "(!request.skill.isEmpty() || !request.intentKind.isEmpty())" not in launch
    # When the bridge exists, the frozen capture remains bound to its exact
    # instance and session before AgentHost receives that target.
    assert "instance != request.contextInstanceId" in launch
    assert "captureSessionId != request.requestedCaptureSessionId" in launch


def test_clarification_origin_uses_full_request_digest_without_prefix_truncation() -> None:
    header = _source("AIAssistant.h")
    source = _source("AIAssistant.cpp")
    binding = _section(
        source,
        "void AIAssistant::bindClarificationOrigin(",
        "void AIAssistant::updateClarificationUI()",
    )
    persistence = _section(
        source,
        "bool AIAssistant::clarificationFromJSON(",
        "void AIAssistant::bindClarificationOrigin(",
    )
    prompt = _section(
        source,
        "QString AIAssistant::conversationPrompt(",
        "void AIAssistant::advanceChatContextFreeze()",
    )

    assert "QString originRequestSha256" in header
    assert "clarificationOriginSha256(originRequest)" in binding
    assert "originRequest.size() <= MaxClarificationOriginChars" in binding
    assert ".left(MaxClarificationOriginChars)" not in binding
    assert 'object.value(lit("originRequestSha256"))' in persistence
    assert "clarification.originRequestSha256 != textSha256" in persistence
    assert 'object[lit("originRequestSha256")]' in persistence
    assert 'response[lit("originRequestSha256")]' in prompt


def test_chat_process_startup_is_async_and_contains_children_before_prompt() -> None:
    header = _source("AIAssistant.h")
    source = _source("AIAssistant.cpp")
    launch = _section(
        source,
        "void AIAssistant::launchChat(",
        "void AIAssistant::handleChatProcessStarted(",
    )
    started = _section(
        source,
        "void AIAssistant::handleChatProcessStarted(",
        "void AIAssistant::failChatProcessStart(",
    )
    failed_start = _section(
        source,
        "void AIAssistant::failChatProcessStart(",
        "void AIAssistant::adjustQueuedChat(",
    )
    cancel = _section(
        source,
        "void AIAssistant::cancelChat(",
        "void AIAssistant::clearCaptureTasks(",
    )
    finish = _section(
        source,
        "void AIAssistant::finishChatProcess(",
        "void AIAssistant::drainChatTaskEvents(",
    )

    assert "execution->startTimer->setSingleShot(true);" in launch
    assert "execution->startTimer->setInterval(3000);" in launch
    assert "&QProcess::started" in launch
    assert "&QProcess::errorOccurred" in launch
    assert "&QProcess::finished" in launch
    assert "waitForStarted" not in launch
    assert "waitForFinished" not in launch

    # The provider must not receive a byte until Windows Job containment succeeds.
    assert started.index("attachKillOnCloseJob(") < started.index("process->write(")
    assert started.index("process->write(") < started.index("process->closeWriteChannel();")

    # A late notification from a timed-out/cancelled process cannot complete a newer task.
    assert failed_start.index("disconnect(process, NULL, this, NULL);") < failed_start.index(
        "execution->process = NULL;"
    )
    assert "waitForFinished" not in failed_start
    assert "execution->startTimer->stop();" in cancel
    assert "execution->startTimer->stop();" in finish
    assert "QByteArray pendingInput" in header
    assert "bool promptSent = false" in header


def test_chat_cancellation_requests_a_durable_agent_terminal_before_force_stop() -> None:
    header = _source("AIAssistant.h")
    source = _source("AIAssistant.cpp")
    launch = _section(
        source,
        "void AIAssistant::launchChat(",
        "void AIAssistant::handleChatProcessStarted(",
    )
    shutdown = _section(
        source,
        "void AIAssistant::Shutdown()",
        "void AIAssistant::appendSystem(",
    )
    cancel_request = _section(
        source,
        "void AIAssistant::requestChatCancellation(",
        "void AIAssistant::clearChatCancelFile(",
    )
    cancel = _section(
        source,
        "void AIAssistant::cancelChat(",
        "void AIAssistant::clearCaptureTasks(",
    )

    assert "void requestChatCancellation(ChatExecution *execution);" in header
    assert "QString cancelFile" in header
    assert 'arguments << lit("--cancel-file") << cancelFilePath' in launch
    assert launch.index("cancelFile.setAutoRemove(false);") < launch.index(
        "cancelFile.close();"
    )
    assert 'signal.write("cancel\\n")' in cancel_request
    assert shutdown.index("requestChatCancellation(chatExecution(taskId));") < shutdown.index(
        "QElapsedTimer cancellationDeadline;"
    )
    assert "2500 - int(cancellationDeadline.elapsed())" in shutdown
    assert shutdown.index('cancelChat(lit("interrupted"), false, taskId,') < shutdown.index(
        "forceStopProcessesForDestruction();"
    )
    assert cancel.index("requestChatCancellation(execution);") < cancel.index(
        "waitForFinished(cooperativeWaitMs)"
    )
    assert cancel.index("waitForFinished(cooperativeWaitMs)") < cancel.index(
        "closeKillOnCloseJob(execution->jobHandle);"
    )


def test_conversations_own_model_affinity_and_run_in_independent_process_lanes() -> None:
    header = _source("AIAssistant.h")
    source = _source("AIAssistant.cpp")
    persistence = _section(
        source,
        "QJsonObject AIAssistant::conversationToJSON(",
        "void AIAssistant::loadConversations()",
    )
    selection = _section(
        source,
        "void AIAssistant::refreshProfiles(",
        "bool AIAssistant::connectionMutationAllowed(",
    )
    start = _section(source, "void AIAssistant::startChat(", "void AIAssistant::startNextChat(")
    next_chat = _section(
        source,
        "void AIAssistant::startNextChat(",
        "void AIAssistant::launchChat(",
    )
    launch = _section(
        source,
        "void AIAssistant::launchChat(",
        "void AIAssistant::handleChatProcessStarted(",
    )

    assert header.count("QString profileId;") >= 2
    assert header.count("QString modelId;") >= 2
    assert "QHash<QString, ChatExecution *> m_ChatExecutions" in header
    assert "QProcess *m_ChatProcess" not in header
    assert "QString m_ChatTaskId" not in header

    assert 'object[lit("profileId")] = thread.profileId;' in persistence
    assert 'object[lit("modelId")] = thread.modelId;' in persistence
    assert 'object.value(lit("profileId"))' in persistence
    assert 'object.value(lit("modelId"))' in persistence
    assert "syncConnectionSelectionFromConversation();" in source
    assert "repairConversationConnectionSelections();" in source
    # A store this session could not read is never written back, so a transient read
    # failure (file locked, size cap, newer schema) cannot replace the user's history
    # with the blank thread the panel creates to stay usable.
    assert "if((repairedConnectionSelections || removedDeletedConversations) && !m_ConversationStoreReadOnly)" in source
    assert "if(m_ConversationStoreReadOnly)\n    return;" in source
    assert "m_ConversationStoreReadOnly = !storeError.isEmpty();" in source
    assert "if(repairedConnectionSelections && !m_LoadingConversations)" in selection
    assert "Wait for active AI tasks before switching models." not in selection
    assert "Wait for active AI tasks before switching connections." not in selection

    # Provider affinity is copied before the request enters the queue and only that copy is used.
    assert "request.profileId = submittedProfile;" in start
    assert "request.modelId = submittedModel;" in start
    assert 'lit("--profile"), request.profileId' in launch
    assert 'arguments << lit("--model") << request.modelId' in launch
    assert (
        "configureProcess(chatProcess, request.profileId, "
        "&execution->processInformation);"
    ) in launch
    assert "request.clarificationResponse, request.attachments," in launch
    assert "request.profileId)" in launch
    assert "m_ChatQueue.size() >= MaxPendingChats" in start
    assert "pendingForConversation" not in start

    # One conversation remains ordered, but a busy/frozen lane cannot block another conversation.
    assert "busyConversations.contains(request.conversationId)" in next_chat
    assert "busyConversations.insert(request.conversationId);" in next_chat
    frozen_barrier = next_chat.index(
        "request.contextFreezeRequired && request.contextSnapshot.isEmpty()"
    )
    frozen_advance = next_chat.index("++index;", frozen_barrier)
    assert next_chat.index(
        "busyConversations.insert(request.conversationId);", frozen_barrier
    ) < frozen_advance
    assert "++index;" in next_chat
    assert "launchChat(request);" in next_chat
    assert "m_ChatExecutions" in next_chat
    assert "m_ChatProcess" not in next_chat

    event_poll = _section(
        source,
        "qint64 AIAssistant::pollTaskEvent(",
        "void AIAssistant::consumePublicAgentMessageEvent(",
    )
    assert 'm_TaskConversationIds.value(taskId)' in event_poll


def test_development_prefers_its_deployed_agenthost_with_source_fallback() -> None:
    source = _source("AIAssistant.cpp")
    finder = _section(
        source,
        "QString AIAssistant::findAgentCLI() const",
        "QString AIAssistant::findBridgeInstance() const",
    )
    development = _section(finder, "#if !defined(RELEASE)", "#endif")

    assert "../../replaylab/.venv/Scripts/replaylab-ai.exe" in development
    assert (finder.index('environmentValue("REPLAYLAB_AI_CLI")') <
            finder.index('replaylab-runtime-manifest.json') < finder.index(development))


def test_agenthost_response_locale_follows_ui_language_and_resolves_system_setting() -> None:
    source = _source("AIAssistant.cpp")
    configure = _section(
        source,
        "void AIAssistant::configureProcess(",
        "bool AIAssistant::attachKillOnCloseJob(",
    )

    configured = "QString responseLocale = m_Ctx.Config().UILanguage.trimmed();"
    system_setting = (
        'responseLocale.compare(lit("system"), Qt::CaseInsensitive) == 0'
    )
    system_locale = "responseLocale = QLocale::system().name();"
    exported = (
        'environment.insert(lit("REPLAYLAB_RESPONSE_LOCALE"), responseLocale);'
    )

    assert configured in configure
    assert system_setting in configure
    assert system_locale in configure
    assert exported in configure
    assert configure.index(configured) < configure.index(system_setting)
    assert configure.index(system_setting) < configure.index(system_locale)
    assert configure.index(system_locale) < configure.index(exported)


def test_chat_context_freeze_is_serial_async_and_file_polled() -> None:
    header = _source("AIAssistant.h")
    source = _source("AIAssistant.cpp")
    start = _section(source, "void AIAssistant::startChat(", "void AIAssistant::startNextChat(")
    next_chat = _section(
        source, "void AIAssistant::startNextChat(", "void AIAssistant::launchChat("
    )
    advance = _section(
        source,
        "void AIAssistant::advanceChatContextFreeze()",
        "void AIAssistant::updateChatContextFreezeTimer()",
    )
    poll = _section(
        source,
        "void AIAssistant::pollChatContextFreeze()",
        "void AIAssistant::failQueuedChatContextFreeze(",
    )
    fail = _section(
        source,
        "void AIAssistant::failQueuedChatContextFreeze(",
        "void AIAssistant::discardChatContextFreeze(",
    )
    edit = _section(
        source, "void AIAssistant::editQueuedChat(", "void AIAssistant::deleteQueuedChat("
    )

    assert "QTimer *m_ChatContextFreezeTimer" in header
    assert "requestedCaptureSessionId" in header
    assert "requestedEventId" in header
    assert "requestedSelectedEventId" in header
    assert "findBridgeBinding(" in start
    assert "m_Ctx.CurEvent()" in start
    assert "m_Ctx.CurSelectedEvent()" in start
    assert "freezeChatContext" not in start
    assert "CurrentContextJSON" not in start

    # One active job is a hard Begin barrier; queued requests retain native submit identities.
    assert "!m_ChatContextFreezeJobId.isEmpty()" in advance
    assert advance.count("BeginCurrentContextFreezeJSON(") == 1
    assert "requestedCaptureSessionId" in advance
    assert "requestedEventId" in advance
    assert "requestedSelectedEventId" in advance
    assert "contextSnapshot.isEmpty()" in next_chat

    # Real-time polling never enters Python or waits on replay/GIL ownership.
    assert "QFile resultFile(" in poll
    assert "ValidateCurrentContextFreezeResultPath(" in poll
    assert "PollCurrentContextFreezeJSON(" not in poll
    assert "AbandonCurrentContextFreezeJSON(" not in poll
    assert "BlockInvoke" not in poll
    assert "processEvents" not in source
    assert "m_ChatContextFreezeTimedOut = true;" in poll
    assert "keep polling its terminal file as a drain barrier" in poll

    # Timeout and a later terminal envelope can both reach the failure helper.
    # Only the first call removes the matching queued task; a later call returns
    # before appendSystem, so one failed freeze produces one transcript error.
    empty_guard = fail.index("if(conversationId.isEmpty())")
    append = fail.index("appendSystem(diagnostic, true, conversationId);")
    assert empty_guard < append
    assert "return;" in fail[empty_guard:append]
    assert fail.count("appendSystem(") == 1

    # Editing replaces only the local version and waits for any obsolete active job to drain.
    assert "currentContextSnapshot.clear();" in edit
    assert "currentContextInstanceId.clear();" in edit
    assert "freezeChatContext" not in edit
    assert "CurrentContextJSON" not in edit
    assert "advanceChatContextFreeze();" in edit
