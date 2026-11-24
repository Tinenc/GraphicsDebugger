"""Vendor-native single-Agent engine for subscription connections.

ACP/Codex own the reasoning loop and call one task-scoped Bread MCP server
directly. AgentHost supplies target hints, effect boundaries, event projection
and durable tool receipts. Shader organization stays in that session until the
Agent records a current self-review or a concrete incomplete outcome.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .conversation import (
    PROVIDER_NATIVE_DOCUMENT_MEDIA_TYPES,
    PROVIDER_NATIVE_IMAGE_MEDIA_TYPES,
    ProviderBinaryAttachment,
    ProviderPathAttachment,
    prepare_provider_conversation_input,
    parse_conversation_prompt,
)
from .catalog import BREAD_MCP_SERVER_NAME
from .errors import ConfigurationError, ConnectorError, ProviderIncidentError, ProviderRequestError
from .events import TaskEventWriter
from .response_language import configured_interface_locale, infer_response_language
from . import native_vendor as vendor
from .acp_session import ACPMainAgentSession, PromptResult
from .agent_diagnostics import configure_logging, get_logger, log_exception, public_error
from .native_mcp import NativeMCPRoute, build_native_mcp_route, native_codex_argv
from .policy import ordered_tool_names
from .agent_contracts import OrchestratorConfig, RunRequest, RunResult
from .agent_progress import ProgressReporter
from .task_input import prepare_native_task_input, native_task_reference
from .output_continuation import (ACP_TURN_LIMIT_REASONS, OutputContinuationLoop,
                                  continuation_prompt, task_work_fingerprint, merge_answer_text)
from .agent_prompts import (
    main_agent_user_prompt,
    native_agent_system_prompt,
)
from .agent_protocol import ClarificationOption, ClarificationRequest
from .agent_context import ProviderContext
from .tool_scope import selected_shader_target_for_intent
from .agent_registry import CatalogView, SkillInfo
from .agent_report import TaskReportStore, read_task_report_sidecars
from .task_review import (TaskReviewDecision, TaskReviewLoop, TaskActivityTracker, completion_review_state,
                          emit_task_review, shader_review_state as review_state, work_identity)
from .draft_recovery_reporting import emit_draft_recovery, recovery_note, retained_task_outputs

_QUESTION_BLOCK_RE = re.compile(
    r"\[REPLAYLAB QUESTION(?:\s+(single|multiple))?\]\s*(.*?)\s*"
    r"\[/REPLAYLAB QUESTION\]",
    re.IGNORECASE | re.DOTALL,
)
_QUESTION_OPTION_RE = re.compile(r"^\s*([1-6])[.)]\s+(.+?)\s*$")
_ACP_TRANSPORT_RETRY_LIMIT = 2
_DURABLE_RECEIPT_SETTLE_SECONDS = 2.0
_DURABLE_RECEIPT_POLL_SECONDS = 0.1


def _tool_state_revision(state: Mapping[str, Any]) -> tuple[Any, ...]:
    """Fingerprint the durable tool ledger while an interrupted call settles."""

    facts = state.get("toolFacts")
    facts = facts if isinstance(facts, list) else []
    tail = facts[-1] if facts and isinstance(facts[-1], Mapping) else {}
    return (
        state.get("updatedUnixNs"), len(facts),
        tail.get("tool"), tail.get("outcome"), tail.get("summary"),
    )


def _recorded_host_tool_calls(state: Mapping[str, Any]) -> int:
    """Count settled Host calls, including repeats, from the durable receipt ledger.

    ``toolsUsed`` lists distinct names, not calls. The ledger retains at most 128
    tool names and excludes local Skill/directory calls and unsettled calls.
    """

    stats = state.get("toolStats")
    if not isinstance(stats, Mapping):
        return 0
    return sum(
        calls
        for row in stats.values()
        if isinstance(row, Mapping)
        for calls in (row.get("calls"),)
        if isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0
    )


class NativeAgentEngine:
    """Run one vendor-native Agent while keeping Host policy deterministic."""

    def __init__(
        self,
        *,
        catalog: CatalogView,
        config: OrchestratorConfig | None = None,
        acp_session_factory: Callable[..., ACPMainAgentSession] = ACPMainAgentSession,
        codex_connector_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.catalog = catalog
        self.config = config or OrchestratorConfig()
        self._acp_session_factory = acp_session_factory
        self._codex_connector_factory = codex_connector_factory
        self.log = configure_logging(self.config.log_root)

    async def run(self, request: RunRequest) -> RunResult:
        writer = TaskEventWriter(self.config.event_root, request.task_id)
        task_id = writer.task_id
        event_root = writer.root.resolve()
        profile = request.profile
        reporter = ProgressReporter(
            writer,
            profile_id=profile.id,
            backend=profile.provider.protocol,
            strategy="vendor-native-agent/v1",
        )
        reporter.queued(
            prompt_chars=len(request.prompt),
            preset=profile.preset,
            profile_title=profile.title or profile.preset,
            model=request.model or profile.model,
            conversation_id=request.conversation_id or parse_conversation_prompt(request.prompt).conversation_id,
        )
        from .user_control import requests_pause, pause_reply
        if requests_pause(request.prompt):
            text = pause_reply(request.prompt)
            # A pause acknowledges control of this turn. It does not certify
            # the unfinished restoration or trigger its recovery/self-review.
            reporter.final_answer(text)
            reporter.completed(text=text, terminal_status="completed")
            return RunResult(task_id=task_id, profile_id=profile.id,
                executor=profile.provider.title, text=text, terminal_status="completed",
                core="vendor-native-agent/v1")
        try:
            return await asyncio.wait_for(self._execute(
                request, task_id=task_id, event_root=event_root, reporter=reporter,
            ), timeout=self.config.main_timeout)
        except asyncio.CancelledError:
            reporter.cancelled()
            raise
        except Exception as error:
            if isinstance(error, asyncio.TimeoutError):
                timeout_error = ConnectorError(
                    f"Vendor-native Agent task exceeded configured runtime limit ({self.config.main_timeout:.0f}s)"
                )
                timeout_error.__cause__ = error
                error = timeout_error
            log_exception(self.log, f"native task {task_id} failed", error)
            message = public_error(error)
            reporter.failed(message, cause=error)
            return RunResult(
                task_id=task_id,
                profile_id=profile.id,
                executor=profile.provider.title,
                text="",
                terminal_status="failed",
                error=message,
                core="vendor-native-agent/v1",
            )

    async def _execute(
        self,
        request: RunRequest,
        *,
        task_id: str,
        event_root: Path,
        reporter: ProgressReporter,
    ) -> RunResult:
        profile = request.profile
        protocol = str(profile.provider.protocol)
        if profile.provider.kind != "agent" or protocol not in {"acp", "codex-app-server"}:
            raise ConfigurationError(
                f"vendor-native engine does not support profile protocol '{protocol}'"
            )
        if protocol == "acp" and profile.options.get("allow_unsandboxed_agent") is not True:
            raise ConfigurationError(
                "ACP agents run outside a sandbox; set allow_unsandboxed_agent=true for this connection"
            )

        envelope = parse_conversation_prompt(request.prompt)
        current = envelope.current_request
        conversation_id = request.conversation_id or envelope.conversation_id
        clarification_answer = ""
        if envelope.clarification_response is not None:
            answer = envelope.clarification_response
            clarification_answer = (
                f"clarification {answer.clarification_id}: selected option(s) = "
                f"[{', '.join(answer.option_ids)}]"
                + (f"; free text = {answer.other_text}" if answer.other_text else "")
            )
        language = infer_response_language(
            current, fallback_locale=configured_interface_locale())
        context = ProviderContext(
            profile=profile,
            model=request.model or profile.model,
            instance_id=request.instance_id,
            context=dict(request.context or {}),
            intent=request.intent,
            mcp_executable=request.mcp_executable,
            workspace_base=self.config.workspace_base,
            log_root=self.config.log_root,
            main_timeout=self.config.main_timeout,
            task_id=task_id,
            conversation_id=conversation_id,
            inherit_desktop_capabilities=self.config.inherit_desktop_capabilities,
        )
        workspace, turn_root = context.new_turn_workspace()
        receipt_store = TaskReportStore(
            task_id, event_root, conversation_id=conversation_id
        )
        tools_used: list[str] = []
        host_facts: list[dict[str, Any]] = []
        latest_tool_state: dict[str, Any] = {}
        clarification: ClarificationRequest | None = None
        receipt_store.begin_task_review(work_identity({"prompt": request.prompt, "intent": request.intent}),
            marked_render_target=any(item.context and item.context.annotation for item in envelope.request.attachments))
        activity_tracker = TaskActivityTracker(receipt_store)
        review_loop = TaskReviewLoop.for_intent(request.intent)
        review_decision = TaskReviewDecision()
        draft_recovery = None

        def check_self_review(unfinished_tools: bool = False) -> TaskReviewDecision:
            nonlocal review_decision
            review_decision = review_loop.decide(completion_review_state(receipt_store, activity_tracker), review_state(receipt_store))
            if unfinished_tools:
                review_decision = replace(review_decision, action="partial", reason="unfinished-tools")
            return review_decision

        selected_skill = self._select_skill(request)
        reporter.main_started("Vendor-native Agent is analysing the request")
        try:
            route = build_native_mcp_route(
                context,
                workspace=workspace,
                event_root=event_root,
                allowed_effects=tuple(self.config.allowed_effects),
                skill_roots=tuple(self.config.skill_roots or self.catalog.roots),
                disabled_skills=tuple(self.config.disabled_skills),
                mcp_servers=tuple(self.config.external_mcp_servers),
                editor_python_enabled=self.config.native_shell_enabled,
            )
            visible_catalog = CatalogView(
                skills={
                    skill_id: skill
                    for skill_id, skill in self.catalog.skills.items()
                    if skill_id not in self.config.disabled_skills
                },
                roots=self.catalog.roots,
            )
            system_prompt = native_agent_system_prompt(
                catalog=visible_catalog,
                request=current,
                context=request.context,
                response_language=language,
                tool_names=tuple(
                    ordered_tool_names(route.visible_tools or route.allowed_tools)
                ) if route else (),
                protocol=(
                    "claude-acp"
                    if profile.preset == "claude-code-acp"
                    else protocol
                ),
                effects=tuple(sorted(route.effects)) if route else (),
                loaded_skill=selected_skill,
            )
            provider_input = prepare_provider_conversation_input(
                request.prompt,
                provider_name=profile.provider.title,
                native_image_media_types=(
                    PROVIDER_NATIVE_IMAGE_MEDIA_TYPES if protocol == "acp" else None
                ),
                native_file_media_types=(
                    PROVIDER_NATIVE_DOCUMENT_MEDIA_TYPES if protocol == "acp" else None
                ),
                native_path_references=protocol == "codex-app-server",
            )
            user_prompt = main_agent_user_prompt(
                request=provider_input.text,
                history=(),
                clarification_answer=clarification_answer,
                selected_shader_target=selected_shader_target_for_intent(
                    request.intent, request.context
                ),
            )
            user_prompt, route = prepare_native_task_input(user_prompt, route, profile=profile, model=context.model)
            first_prompt = system_prompt + "\n\n" + user_prompt
            if self.config.inherit_desktop_capabilities:
                from .desktop_capabilities import capability_guidance
                first_prompt += capability_guidance(self.config.desktop_capability_summary, native_provider=True)

            def sync_tool_receipts() -> dict[str, Any]:
                nonlocal tools_used, host_facts, latest_tool_state
                receipt_store.sync()
                _unused_report, state_payload = read_task_report_sidecars(
                    event_root, task_id
                )
                latest_tool_state = (
                    dict(state_payload) if isinstance(state_payload, Mapping) else {}
                )
                raw_tools = (state_payload or {}).get("toolsUsed", [])
                if isinstance(raw_tools, list):
                    tools_used = list(
                        dict.fromkeys(
                            str(item) for item in raw_tools if str(item).strip()
                        )
                    )
                raw_facts = (state_payload or {}).get("toolFacts", [])
                host_facts = [dict(item) for item in raw_facts if isinstance(item, dict)][-24:] if isinstance(raw_facts, list) else []
                paths = (state_payload or {}).get("artifactPaths", [])
                if isinstance(paths, list) and paths:
                    host_facts.append({
                        "tool": "Host task artifact ledger", "outcome": "observed paths",
                        "summary": json.dumps(paths[:16], ensure_ascii=False)[:1800],
                        "truncated": len(paths) > 16,
                    })
                return dict(latest_tool_state)

            def capture_elicitation(message: str, mode: Any) -> None:
                nonlocal clarification
                parsed = clarification_from_elicitation(message, mode)
                if parsed is not None:
                    clarification = parsed

            def bind_clarification(
                parsed: ClarificationRequest | None,
            ) -> ClarificationRequest | None:
                return parsed

            if protocol == "acp":
                result, clarification = await self._run_acp(
                    context=context,
                    workspace=workspace,
                    route=route,
                    first_prompt=first_prompt,
                    attachments=provider_input.attachments,
                    current_request=current,
                    sync_tool_receipts=sync_tool_receipts,
                    tools_used=lambda: tuple(tools_used),
                    host_facts=lambda: tuple(host_facts),
                    reporter=reporter,
                    capture_elicitation=capture_elicitation,
                    clarification=lambda: clarification,
                    bind_clarification=bind_clarification,
                    check_self_review=check_self_review,
                    activity_tracker=activity_tracker,
                )
                text = result.final_text.strip()
            else:
                text, clarification = await self._run_codex(
                    context=context,
                    workspace=workspace,
                    route=route,
                    first_prompt=first_prompt,
                    attachments=provider_input.attachments,
                    path_attachments=provider_input.path_attachments,
                    selected_skill=selected_skill,
                    sync_tool_receipts=sync_tool_receipts,
                    reporter=reporter,
                    bind_clarification=bind_clarification,
                    check_self_review=check_self_review,
                    activity_tracker=activity_tracker,
                )
            final_tool_state = await self._reconcile_durable_host_state(
                sync_tool_receipts,
                in_flight_tools=(),
            )
        finally:
            draft_recovery = context.cleanup_turn_workspace(turn_root)
            emit_draft_recovery(reporter, draft_recovery)

        worked = bool(tools_used)
        provider_interrupted = bool(
            protocol == "acp" and getattr(result, "provider_incident", "")
        )
        provider_output_limited = bool(
            protocol == "acp" and getattr(result, "stop_reason", "") == "max_tokens"
        )
        provider_turn_limited = bool(
            protocol == "acp" and getattr(result, "stop_reason", "") in ACP_TURN_LIMIT_REASONS
        )
        if clarification is not None:
            # The block itself is markup the user must never read, but whatever the
            # Agent said around it may be a real partial result. Keep that and fall
            # back to the question only when there is nothing else; the panel renders
            # the question and its options from the clarification payload regardless.
            remaining = _QUESTION_BLOCK_RE.sub("", str(text or "")).strip()
            text = remaining or clarification.question
            status = "needs-user-input"
        else:
            reviewed_explanation = (review_decision.reviewed_explanation()
                                    if not provider_interrupted and not provider_turn_limited else '')
            if reviewed_explanation:
                text = reviewed_explanation
            missing_answer = not text
            interruption_note = (
                "模型连接在重试后仍然中断；Bread 已保留并重新核对 Host 收据，"
                "但没有把中断诊断当成任务答案。"
                if language == "zh_CN"
                else
                "The model connection was still interrupted after retrying. "
                "Bread preserved and reconciled the Host receipts instead of "
                "using the provider diagnostic as the task answer."
            )
            if not text:
                if provider_interrupted:
                    text = interruption_note
                else:
                    text = "任务没有返回可显示的结果。" if language == "zh_CN" else "The task returned no displayable result."
            if provider_interrupted:
                status = "partial" if worked else "failed"
                if not missing_answer:
                    text += "\n\n" + interruption_note
            elif provider_output_limited:
                status = "partial"
                limit_note = (
                    "模型达到本轮输出上限，回答可能被截断。已完成的修改仍保留。"
                    if language == "zh_CN"
                    else "The model reached this turn's output limit; its answer may be truncated. Completed edits remain saved."
                )
                text = (text + "\n\n" + limit_note).strip()
            elif provider_turn_limited:
                status = "partial"
            elif missing_answer:
                status = "partial" if worked else "failed"
            else:
                status = "completed"
            if review_decision.action == "partial" and not provider_interrupted and not provider_turn_limited:
                status = "partial"
                text = review_decision.incomplete_text(language, text)
            elif review_decision.action == "accept" and status == "completed" and not reviewed_explanation:
                note = review_decision.unchanged_note(language)
                if note:
                    text += "\n\n" + note
        if status == "partial" or retained_task_outputs(draft_recovery):
            note = recovery_note(draft_recovery, language)
            if note:
                text += "\n\n" + note
        total_tool_calls = _recorded_host_tool_calls(final_tool_state)
        reporter.main_completed(
            f"Vendor-native Agent finished ({total_tool_calls} recorded Bread Host tool call(s))"
        )
        reporter.final_answer(text)
        reporter.completed(
            text=text,
            terminal_status=status,
            clarification=clarification,
            context=request.context,
            instance_id=request.instance_id,
        )
        if request.instance_id and status != "needs-user-input":
            try:
                from .deep_engine import (
                    BreadMCPTools,
                    _maybe_release_task_shader_occupancy,
                )

                release_session = BreadMCPTools(
                    request, replace(self.config, external_mcp_servers=()), event_root,
                )
                async with release_session as tools:
                    release = _maybe_release_task_shader_occupancy(
                        tools, terminal_status=status
                    )
                    if inspect.isawaitable(release):
                        await release
            except Exception as error:
                get_logger().warning(
                    "native idle shader occupancy release failed: %s",
                    public_error(error),
                )
        return RunResult(
            task_id=task_id,
            profile_id=profile.id,
            executor=profile.provider.title,
            text=text,
            terminal_status=status,
            clarification=clarification,
            core="vendor-native-agent/v1",
        )

    async def _run_acp(
        self,
        *,
        context: ProviderContext,
        workspace: Path,
        route: NativeMCPRoute | None,
        first_prompt: str,
        attachments: tuple[ProviderBinaryAttachment, ...],
        current_request: str,
        sync_tool_receipts: Callable[[], dict[str, Any]],
        tools_used: Callable[[], tuple[str, ...]],
        host_facts: Callable[[], tuple[dict[str, Any], ...]],
        reporter: ProgressReporter,
        capture_elicitation: Callable[[str, Any], None],
        clarification: Callable[[], ClarificationRequest | None],
        bind_clarification: Callable[
            [ClarificationRequest | None], ClarificationRequest | None
        ],
        check_self_review: Callable[[bool], TaskReviewDecision],
        activity_tracker: TaskActivityTracker,
    ) -> tuple[PromptResult, ClarificationRequest | None]:
        native_write = "workspace" in (route.effects if route else self.config.allowed_effects)
        allowed_tools = (
            route.visible_tools or route.allowed_tools if route else frozenset()
        )
        # The client supports scoped fs callbacks for every ACP adapter. Keep
        # ambient built-ins closed on adapters without a verified path policy.
        vendor.prepare_workspace(context.profile, workspace, allowed_tools,
                                 inherit_desktop_capabilities=self.config.inherit_desktop_capabilities)
        if context.profile.preset == "cursor-acp" and not self.config.inherit_desktop_capabilities:
            from .process_env import ensure_cursor_profile_config, ensure_cursor_workspace
            ensure_cursor_profile_config(context.profile, workspace=workspace, writable=native_write)
            ensure_cursor_workspace(workspace, writable=native_write)
        elif context.profile.preset == "cursor-acp":
            from .process_env import release_cursor_managed_tool_restrictions
            release_cursor_managed_tool_restrictions(context.profile)
        if context.profile.preset == "claude-code-acp":
            vendor.copy_claude_skills(
                workspace,
                [
                    (skill.id, skill.path)
                    for skill in self.catalog.skills.values()
                    if skill.path is not None and skill.id not in self.config.disabled_skills
                ],
            )
            if route is not None:
                vendor.write_claude_mcp_config(
                    workspace,
                    command=route.command,
                    args=route.args,
                    env=route.env,
                    # Configured servers are attached once inside the Bread
                    # proxy, with lazy contracts and explicit external authority.
                    extra_servers=(),
                )
        stderr_path = (
            context.log_root or context.workspace_root().parent / "logs"
        ) / f"acp-native-{context.task_id}.stderr.log"

        def new_session() -> ACPMainAgentSession:
            return self._acp_session_factory(
                command=vendor.runtime_argv(
                    context.profile,
                    context.agent_argv(),
                    workspace,
                    allowed_tools,
                    inherit_desktop_capabilities=self.config.inherit_desktop_capabilities,
                ),
                cwd=workspace,
                env=context.agent_env(
                    vendor.main_agent_env(context.profile, context.main_timeout)
                ),
                allowed_tools=allowed_tools,
                native_workspace_root=workspace,
                workspace_write=native_write,
                terminal_execution=self.config.native_shell_enabled,
                mcp_stdio_command=route.command if route else "",
                mcp_stdio_args=route.args if route else (),
                mcp_stdio_env=route.env if route else None,
                on_elicitation=capture_elicitation,
                timeout=context.main_timeout,
                authentication_method=context.profile.provider.acp_auth_method,
                provider_id=context.profile.preset,
                model=context.model,
                stderr_path=stderr_path,
                session_meta=vendor.session_meta(context.profile,
                    inherit_desktop_capabilities=self.config.inherit_desktop_capabilities),
                extra_mcp_servers=(),
            )

        def asked(answer: Any) -> Any:
            """The question this turn produced, however the vendor asked it.

            Only Claude Code is told to use its own question tool; every other ACP
            vendor is told to emit the plain-text block, because most have no
            elicitation form. Without this the block is never parsed on this route:
            the run ends "completed" and the user reads the raw markup.
            """

            found = clarification()
            if found is not None:
                return found
            return bind_clarification(clarification_from_numbered_text(
                str(getattr(answer, "final_text", getattr(answer, "text", "")) or "")))

        transport_retries = 0
        output_loop = OutputContinuationLoop(task_work_fingerprint(activity_tracker.store))
        prompt = first_prompt
        prompt_attachments = attachments
        last_result = PromptResult("", [""], "provider_pending", 0)
        limited_text = ""
        compacted_work: set[str] = set()

        while True:
            restart_transport = False
            session = new_session()
            unfinished_tools: set[str] = set()
            async with session:
                while True:
                    try:
                        result = await self._prompt_acp(
                            session,
                            prompt,
                            reporter,
                            attachments=prompt_attachments,
                            unfinished_tools=unfinished_tools,
                            activity_tracker=activity_tracker,
                        )
                    except ProviderRequestError as error:
                        compact = getattr(session, "compact_context", None)
                        fingerprint = task_work_fingerprint(activity_tracker.store)
                        if (error.error_code != "provider-context" or unfinished_tools or not callable(compact)
                                or fingerprint in compacted_work):
                            raise
                        compacted_work.add(fingerprint)
                        if not await compact():
                            raise
                        sync_tool_receipts()
                        get_logger().info("ACP input window recovered using the advertised compact command")
                        prompt = (
                            "The provider rejected an oversized input and its context has now been compacted. "
                            "Continue the existing task from retained state with smaller reads and bounded tool calls. "
                            "Do not restart the task or repeat completed effects. Recover exact data from retained "
                            "files and receipts; preserve the original user constraints."
                        ) + native_task_reference(route)
                        prompt_attachments = ()
                        continue
                    except ProviderIncidentError as incident:
                        await self._reconcile_durable_host_state(
                            sync_tool_receipts,
                            in_flight_tools=(
                                incident.in_flight_tools or ("provider-turn",)
                            ),
                        )
                        if transport_retries >= _ACP_TRANSPORT_RETRY_LIMIT:
                            safe_partial = incident.partial_text.strip()
                            if not safe_partial:
                                safe_partial = (
                                    last_result.final_text or last_result.text
                                ).strip()
                            exhausted = PromptResult(
                                safe_partial,
                                [safe_partial],
                                "provider_incident",
                                0,
                                provider_incident=incident.incident_code,
                                transport_retries=transport_retries,
                            )
                            return exhausted, asked(exhausted)
                        transport_retries += 1
                        get_logger().warning(
                            "ACP provider incident %s; reopening a fresh session "
                            "(%d/%d)",
                            incident.incident_code,
                            transport_retries,
                            _ACP_TRANSPORT_RETRY_LIMIT,
                        )
                        previous = (
                            incident.partial_text.strip()
                            or last_result.final_text
                            or last_result.text
                        )
                        prompt = self._provider_recovery_prompt(
                            first_prompt=first_prompt,
                            current_request=current_request,
                            incident_code=incident.incident_code,
                            previous_answer=previous,
                            tools_used=tools_used(),
                            host_facts=host_facts(),
                        )
                        # A fresh provider process has no attachment state.
                        prompt_attachments = attachments
                        restart_transport = True
                        break

                    prompt_attachments = ()
                    current_answer = result.final_text
                    # A later reasoning-only truncation must not erase the last
                    # displayable partial answer. A new tool boundary invalidates
                    # that fallback: pre-tool progress is never a conclusion.
                    if result.tool_calls:
                        limited_text = ""
                    if result.stop_reason in ACP_TURN_LIMIT_REASONS:
                        limited_text = merge_answer_text(limited_text, result.final_text)
                        if limited_text and not unfinished_tools:
                            result = replace(result, text=limited_text, segments=[limited_text])
                    else:
                        if (not result.final_text.strip() and limited_text and not unfinished_tools
                                and result.stop_reason == "end_turn"):
                            return last_result, asked(last_result)
                        if result.final_text.strip() and limited_text and result.stop_reason == "end_turn":
                            answer = merge_answer_text(limited_text, result.final_text)
                            result = replace(result, text=answer, segments=[answer])
                        limited_text = ""
                    last_result = result
                    sync_tool_receipts()
                    question = asked(result)
                    if result.stop_reason in ACP_TURN_LIMIT_REASONS and question is None and not unfinished_tools:
                        # This is an explicit protocol truncation, not a Host
                        # judgement about whether the Agent's work is good enough.
                        # Keep the same session and receipts so completed effects
                        # and Skill reads are not repeated on a fresh transcript.
                        # Judge the actual new answer, not a fallback copied from
                        # an older turn; replayed text is not forward progress.
                        if not output_loop.should_continue(
                            current_answer, task_work_fingerprint(activity_tracker.store),
                        ):
                            get_logger().warning("ACP turn continuation stalled: reason=%s noProgressTurns=%d",
                                                 result.stop_reason, output_loop.no_progress_turns)
                            return result, question
                        get_logger().info(
                            "ACP turn limited; continuing the same session (reason=%s continuation=%d stalled=%d)",
                            result.stop_reason, output_loop.continuations, output_loop.no_progress_turns,
                        )
                        prompt = continuation_prompt(result.stop_reason) + native_task_reference(route)
                        continue
                    if result.stop_reason == "end_turn" and question is None:
                        output_loop.normal_turn()
                        decision = check_self_review(bool(unfinished_tools))
                        if decision.action == "continue":
                            emit_task_review(reporter, decision)
                            prompt = decision.continuation_prompt() + native_task_reference(route)
                            continue
                    if result.stop_reason in ACP_TURN_LIMIT_REASONS and unfinished_tools:
                        get_logger().warning("ACP turn continuation deferred: reason=%s unfinishedTools=%d",
                                             result.stop_reason, len(unfinished_tools))
                    return result, question
            if restart_transport:
                continue

    @staticmethod
    def _provider_recovery_prompt(
        *,
        first_prompt: str,
        current_request: str,
        incident_code: str,
        previous_answer: str,
        tools_used: tuple[str, ...],
        host_facts: tuple[dict[str, Any], ...],
    ) -> str:
        """Self-contained prompt for a fresh process after a transport loss."""

        durable = {
            "toolsUsed": list(tools_used)[-64:],
            "hostFacts": [dict(item) for item in host_facts[-16:]],
        }
        return (
            first_prompt
            + "\n\n## Provider transport recovery\n"
            + "The previous vendor process ended because of provider incident `"
            + incident_code
            + "`. This is a transport retry. "
            "Continue the original request from the durable Bread Host state below. "
            "Treat successful receipts as already performed: inspect or poll their "
            "current status and never repeat a mutation merely because its response "
            "was interrupted.\n"
            + "Original user request:\n"
            + current_request[:12000]
            + "\n\nLast safe assistant draft (not authoritative):\n"
            + str(previous_answer or "")[:6000]
            + "\n\nDurable Host snapshot:\n"
            + json.dumps(durable, ensure_ascii=False, separators=(",", ":"))[:24000]
        )

    async def _reconcile_durable_host_state(
        self,
        sync_tool_receipts: Callable[[], dict[str, Any]],
        *,
        in_flight_tools: tuple[str, ...],
    ) -> dict[str, Any]:
        """Refresh sidecars after the provider stopped, before choosing a result.

        The MCP proxy writes tool receipts under a durable task lock. A provider
        stream can close a few milliseconds before that write becomes visible.
        When the ACP timeline still had a running tool (or the provider turn died
        before its final tool status), wait for one bounded settle window and
        return the newest state; ordinary successful terminalization pays no wait.
        """

        state = sync_tool_receipts()
        if not in_flight_tools:
            return state
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _DURABLE_RECEIPT_SETTLE_SECONDS
        previous = _tool_state_revision(state)
        stable_reads = 0
        saw_change = False
        while loop.time() < deadline:
            await asyncio.sleep(_DURABLE_RECEIPT_POLL_SECONDS)
            current = sync_tool_receipts()
            revision = _tool_state_revision(current)
            if revision == previous:
                stable_reads += 1
            else:
                saw_change = True
                stable_reads = 0
                previous = revision
            state = current
            if saw_change and stable_reads >= 2:
                break
        return state

    async def _prompt_acp(
        self,
        session: ACPMainAgentSession,
        prompt: str,
        reporter: ProgressReporter,
        *,
        attachments: tuple[ProviderBinaryAttachment, ...] = (),
        unfinished_tools: set[str] | None = None,
        activity_tracker: TaskActivityTracker | None = None,
    ) -> PromptResult:
        def timeline(event: Mapping[str, Any]) -> None:
            if activity_tracker is not None:
                activity_tracker.native_event(event)
            if unfinished_tools is not None:
                _track_unfinished_tools(unfinished_tools, event)
            self.log.debug("native ACP timeline: %s", dict(event))
            self._project_provider_timeline(reporter, event, source="acp")

        return await self._with_heartbeat(
            session.prompt(
                prompt,
                on_delta=reporter.main_message,
                on_reasoning=reporter.main_reasoning,
                on_timeline=timeline,
                attachments=attachments,
            ),
            reporter,
        )

    async def _run_codex(
        self,
        *,
        context: ProviderContext,
        workspace: Path,
        route: NativeMCPRoute | None,
        first_prompt: str,
        attachments: tuple[ProviderBinaryAttachment, ...],
        path_attachments: tuple[ProviderPathAttachment, ...],
        selected_skill: SkillInfo | None,
        sync_tool_receipts: Callable[[], dict[str, Any]],
        reporter: ProgressReporter,
        bind_clarification: Callable[
            [ClarificationRequest | None], ClarificationRequest | None
        ],
        check_self_review: Callable[[bool], TaskReviewDecision],
        activity_tracker: TaskActivityTracker,
    ) -> tuple[str, ClarificationRequest | None]:
        if self._codex_connector_factory is None:
            from .connectors.codex_app_server import CodexAppServerConnector

            connector_factory = CodexAppServerConnector
        else:
            connector_factory = self._codex_connector_factory
        config_overrides = ()
        desktop_runtime_config = {}
        environment = context.agent_env()
        if self.config.inherit_desktop_capabilities:
            from .desktop_capabilities import discover_desktop_capabilities, codex_runtime_overrides, codex_runtime_config
            desktop = discover_desktop_capabilities()
            config_overrides = codex_runtime_overrides(desktop, Path(environment["CODEX_HOME"]))
            desktop_runtime_config = codex_runtime_config(desktop, Path(environment["CODEX_HOME"]),
                declared_mcp_ids=self.config.declared_mcp_ids, disabled_skill_paths=self.config.disabled_skill_paths)
        connector_options = ({"inherit_desktop_capabilities": True, "desktop_runtime_config": desktop_runtime_config}
                             if self.config.inherit_desktop_capabilities else {})
        connector = connector_factory(
            native_codex_argv(context.agent_argv(), route,
                             inherit_desktop_capabilities=self.config.inherit_desktop_capabilities,
                             config_overrides=config_overrides),
            cwd=workspace,
            readable_roots=[workspace],
            workspace_write="workspace" in (route.effects if route else self.config.allowed_effects),
            expected_mcp_servers=(
                frozenset({BREAD_MCP_SERVER_NAME}) if route is not None else frozenset()
            ),
            timeout=context.main_timeout,
            env=environment,
            **connector_options,
        )
        text = ""
        clarification: ClarificationRequest | None = None
        unfinished_tools: set[str] = set()

        def timeline(event: Mapping[str, Any]) -> None:
            activity_tracker.native_event(event)
            _track_unfinished_tools(unfinished_tools, event)
            # Commentary already has a dedicated delta stream. Logging each
            # cumulative progress snapshot writes the same text quadratically
            # and forces a synchronous log flush for every provider token.
            if event.get("activityKind") != "progressNote":
                self.log.debug("native Codex timeline: %s", dict(event))
            self._project_provider_timeline(reporter, event, source="codex-app-server")

        try:
            prompt = first_prompt
            first_turn = True
            while True:
                run = connector.run(
                    prompt,
                    model=context.model or None,
                    skill_name=(
                        selected_skill.id
                        if first_turn and selected_skill is not None and selected_skill.path is not None
                        else None
                    ),
                    skill_path=(
                        selected_skill.path
                        if first_turn and selected_skill is not None and selected_skill.path is not None
                        else None
                    ),
                    on_delta=reporter.main_message,
                    on_reasoning=reporter.main_reasoning,
                    on_timeline=timeline,
                    on_assistant_message=lambda event: self._project_provider_message(
                        reporter, event, source="codex-app-server"
                    ),
                    attachments=attachments if first_turn else (),
                    path_attachments=path_attachments if first_turn else (),
                    keep_thread=True,
                )
                result = await self._with_heartbeat(run, reporter)
                text = str(result.text or "").strip()
                sync_tool_receipts()
                clarification = bind_clarification(clarification_from_numbered_text(text))
                if clarification is not None:
                    break
                decision = check_self_review(bool(unfinished_tools))
                if decision.action != "continue":
                    break
                emit_task_review(reporter, decision)
                prompt = decision.continuation_prompt() + native_task_reference(route)
                first_turn = False
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connector.close(), timeout=10.0)
        return text, clarification

    @staticmethod
    def _project_provider_message(
        reporter: ProgressReporter, event: Mapping[str, Any], *, source: str
    ) -> None:
        """Keep public commentary separate from answer text and private reasoning."""

        from .connectors.codex_app_server import _redact_timeline_text

        if event.get("phase") != "commentary" or not event.get("itemId"):
            return
        text = _redact_timeline_text(event.get("text", ""), limit=4096)
        lifecycle = _redact_timeline_text(event.get("lifecycle", "updated"), limit=32)
        if not text and lifecycle not in {"completed", "failed", "cancelled", "canceled"}:
            return
        data = {
            "source": source, "channel": "message",
            "stream": "provider-observation", "agentId": "main", "agentRole": "main",
            "phase": "commentary", "itemId": _redact_timeline_text(event["itemId"], limit=256),
            "reset": event.get("reset") is True,
            "lifecycle": lifecycle, "activityLifecycle": lifecycle,
        }
        reporter.writer.emit_delta("agent-message-delta", backend=reporter.backend,
                             profile=reporter.profile_id, text=text, data=data)

    @staticmethod
    def _project_provider_timeline(
        reporter: ProgressReporter, event: Mapping[str, Any], *, source: str
    ) -> None:
        """Project provider observations without upgrading them to Host receipts."""

        from .connectors.codex_app_server import _redact_timeline_text

        kind = _redact_timeline_text(event.get("activityKind", ""), limit=64)
        activity_id = _redact_timeline_text(event.get("activityId", ""), limit=256)
        if not kind or not activity_id or kind == "reasoning":
            return  # The exact reasoning callback owns the existing reasoning stream.
        if kind == "progressNote":
            # Commentary deltas already carry the actual message. A cumulative
            # snapshot must not duplicate it; terminal snapshots only close its
            # existing activity, including when no final text delta was emitted.
            if event.get("lifecycle") in {"completed", "failed", "cancelled", "canceled"}:
                NativeAgentEngine._project_provider_message(reporter, {
                    "phase": "commentary", "itemId": activity_id, "text": "",
                    "lifecycle": event["lifecycle"], "reset": False,
                }, source=source)
            return
        target = _redact_timeline_text(event.get("activityTarget", ""), limit=256)
        tool_kind = event.get("toolKind")
        tool_kind = tool_kind if isinstance(tool_kind, str) and tool_kind in {"read", "edit", "delete", "move", "search", "execute", "think", "fetch", "switch_mode", "other"} else ""
        label = target or tool_kind or kind
        arguments = event.get("argumentNames", ())
        names = [_redact_timeline_text(name, limit=96) for name in arguments[:16]] \
            if isinstance(arguments, (list, tuple)) else []
        data: dict[str, Any] = {
            "source": source, "phase": "native", "agentId": "main", "agentRole": "main",
            "activityId": activity_id, "activityKind": kind, "activityTarget": target,
            "status": _redact_timeline_text(event.get("status", "running"), limit=32),
            "lifecycle": _redact_timeline_text(event.get("lifecycle", "updated"), limit=32),
            "argumentNames": names,
            "activityTitleId": "task.detail.public-summary", "activityTitleArgs": [label],
            "activityDetailId": "task.detail.public-summary", "activityDetailArgs": [", ".join(names)],
            "verification": "agentReported", "statusSource": "agent", "hostVerified": False,
        }
        if tool_kind:
            data["toolKind"] = tool_kind
        retry_title = event.get("activityTitleId")
        if (source == "codex-app-server" and kind == "providerRetry"
                and retry_title in {"task.provider.retry-capacity", "task.provider.retry-session"}):
            retry_args = event.get("activityTitleArgs")
            if (isinstance(retry_args, (list, tuple)) and len(retry_args) == 3
                    and all(isinstance(value, (str, int, float)) and len(str(value)) <= 16
                            for value in retry_args)):
                data["activityTitleId"] = retry_title
                data["activityTitleArgs"] = [str(value) for value in retry_args]
                data["statusSource"] = "local"
        if (source == "codex-app-server" and kind == "providerRetry"
                and retry_title == "task.provider.session-ready"
                and event.get("status") == "completed"):
            data.update(activityTitleId=retry_title, activityTitleArgs=[], statusSource="local")
        if source == "codex-app-server" and kind == "nativeCapability":
            # The connector emits this once after verifying the handshake, not
            # from model content. Keep capability downgrade evidence visible.
            data["activityDetailArgs"] = [_redact_timeline_text(event.get("activityDetail", ""), limit=1024)]
            data["nativeWorkspaceCapability"] = {
                "permissionProfile": _redact_timeline_text(event.get("permissionProfile", ""), limit=128),
                "workspaceWrite": event.get("workspaceWrite") is True,
                "networkAccess": event.get("networkAccess") is True,
                "runtimeWorkspaceRoots": [
                    _redact_timeline_text(root, limit=1024)
                    for root in event.get("runtimeWorkspaceRoots", [])[:4]
                    if isinstance(root, str)
                ],
            }
            data["verification"] = "providerHandshake"
            data["statusSource"] = "local"
        observation = event.get("workingFileObservation")
        if (source == "acp" and event.get("verification") == "localFileObserved"
                and event.get("status") == "completed" and isinstance(observation, Mapping)
                and set(observation) - {"coverage"} == {"schema", "scope", "action", "basename", "byteSize", "sha256", "workingCopy", "replayAuthority"}
                and observation.get("schema") == "replaylab-agent-working-file-observation/v1"
                and observation.get("scope") == "owned-workspace"
                and isinstance(observation.get("action"), str)
                and observation.get("action") in {"read", "write"}
                and isinstance(observation.get("basename"), str)
                and observation["basename"] and not any(char in observation["basename"] for char in "/\\\0:")
                and type(observation.get("byteSize")) is int and 0 <= observation["byteSize"] <= 4 * 1024 * 1024
                and isinstance(observation.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", observation["sha256"])
                and observation.get("workingCopy") is True and observation.get("replayAuthority") is False):
            data["workingFileObservation"] = {
                **dict(observation), "basename": _redact_timeline_text(observation["basename"], limit=256)}
            coverage = data["workingFileObservation"].pop("coverage", None)
            if (isinstance(coverage, Mapping) and set(coverage) == {"byteOffset", "returnedBytes", "complete"}
                    and type(coverage.get("byteOffset")) is int and type(coverage.get("returnedBytes")) is int
                    and 0 <= coverage["byteOffset"] <= observation["byteSize"]
                    and 0 <= coverage["returnedBytes"] <= observation["byteSize"] - coverage["byteOffset"]
                    and type(coverage.get("complete")) is bool
                    and coverage["complete"] == (coverage["byteOffset"] == 0 and coverage["returnedBytes"] == observation["byteSize"])):
                data["workingFileObservation"]["coverage"] = dict(coverage)
            data["verification"] = "localFileObserved"
            data["statusSource"] = "local"
        for name in ("elapsedSeconds", "durationMs"):
            value = event.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                data[name] = max(0, value)
        reporter.writer.emit("running", backend=reporter.backend, profile=reporter.profile_id,
                             tool=target, summary=label, data=data)

    async def _with_heartbeat(
        self,
        awaitable: Any,
        reporter: ProgressReporter,
    ) -> Any:
        heartbeat = asyncio.create_task(self._heartbeat(reporter))
        try:
            return await awaitable
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, reporter: ProgressReporter) -> None:
        started = time.monotonic()
        interval = max(0.05, float(self.config.heartbeat_seconds))
        while True:
            await asyncio.sleep(interval)
            reporter.main_heartbeat(
                elapsed_seconds=int(time.monotonic() - started), children=0
            )

    def _select_skill(self, request: RunRequest) -> SkillInfo | None:
        """Pre-load only a Skill explicitly selected by the caller.

        UI intent identifies a target and does not silently choose guidance.
        With no selected Skill, the Agent can inspect the directory and load
        what is useful for the current request.
        """

        if request.skill_name:
            if request.skill_name in self.config.disabled_skills:
                raise ConfigurationError(f"Skill '{request.skill_name}' is disabled")
            return self.catalog.skill(request.skill_name)
        return None


def _plain_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(by_alias=True)
        return dict(result) if isinstance(result, Mapping) else {}
    root = getattr(value, "root", None)
    if root is not None and root is not value:
        return _plain_mapping(root)
    return {}


def _track_unfinished_tools(active: set[str], event: Mapping[str, Any]) -> None:
    if event.get("activityKind") not in {"toolCall", "mcpToolCall", "commandExecution", "fileChange"}:
        return
    identity = str(event.get("activityId") or "")
    if not identity:
        return
    if event.get("status") in {"completed", "failed", "cancelled", "declined"} or event.get("lifecycle") in {"completed", "failed", "cancelled"}:
        active.discard(identity)
    else:
        active.add(identity)


def clarification_from_elicitation(
    message: str,
    mode: Any,
) -> ClarificationRequest | None:
    """Convert the first ACP form select into ReplayLab's panel contract."""

    payload = _plain_mapping(mode)
    if "root" in payload:
        payload = _plain_mapping(payload["root"])
    schema = payload.get("requestedSchema", payload.get("requested_schema", {}))
    schema_payload = _plain_mapping(schema)
    properties = schema_payload.get("properties", {})
    if not isinstance(properties, Mapping):
        return None
    for name, raw_property in properties.items():
        if str(name).endswith("_custom"):
            continue
        prop = _plain_mapping(raw_property)
        option_values: Any = prop.get("oneOf", prop.get("one_of"))
        selection_mode = "single"
        if not isinstance(option_values, list):
            items = _plain_mapping(prop.get("items"))
            option_values = items.get("anyOf", items.get("any_of"))
            selection_mode = "multiple"
        if not isinstance(option_values, list) or not option_values:
            continue
        options: list[ClarificationOption] = []
        for index, raw_option in enumerate(option_values[:6], 1):
            option = _plain_mapping(raw_option)
            label = str(option.get("title") or option.get("const") or "").strip()
            if not label:
                continue
            options.append(
                ClarificationOption(
                    id=f"option-{index}",
                    label=label,
                    description=str(option.get("description") or "").strip(),
                )
            )
        if not options:
            continue
        question = str(prop.get("description") or message or "Please choose an option").strip()
        return ClarificationRequest(
            question=question,
            options=options,
            selection_mode=selection_mode,
            allow_other=True,
        )
    return None


def clarification_from_numbered_text(text: str) -> ClarificationRequest | None:
    """Recognise the explicit Codex numbered-choice fallback block."""

    match = _QUESTION_BLOCK_RE.search(str(text or ""))
    if match is None:
        return None
    # A vendor without a question tool can still ask a multiple-choice question, so
    # it can also say that several answers are allowed. Without this the ACP route
    # could ask "which of these three" and this one could not.
    selection_mode = str(match.group(1) or "single").lower()
    question_lines: list[str] = []
    options: list[ClarificationOption] = []
    for line in match.group(2).splitlines():
        option_match = _QUESTION_OPTION_RE.match(line)
        if option_match is None:
            if line.strip() and not options:
                question_lines.append(line.strip())
            continue
        body = option_match.group(2).strip()
        parts = re.split(r"\s+[—–]\s+", body, maxsplit=1)
        options.append(
            ClarificationOption(
                id=f"option-{option_match.group(1)}",
                label=parts[0].strip()[:240],
                description=(parts[1].strip()[:600] if len(parts) > 1 else ""),
            )
        )
    question = " ".join(question_lines).strip()
    if not question or len(options) < 2:
        return None
    return ClarificationRequest(question=question, options=options[:6],
                                selection_mode=selection_mode, allow_other=True)


__all__ = [
    "NativeAgentEngine",
    "clarification_from_elicitation",
    "clarification_from_numbered_text",
]
