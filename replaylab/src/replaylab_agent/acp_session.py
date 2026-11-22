"""A long-lived ACP session for the main Agent.

Unlike ``connectors.acp.ACPConnector`` (connection diagnostics only), this
session keeps the provider process alive so the main Agent can call the
task-scoped Bread MCP tools in the same context.

It reuses the connector module's SDK loader, client implementation and
timeline projection; only the process/session lifecycle is different.  The
Host-side MCP endpoint is registered under the same server name
(``ReplayLab``) so the connector's fail-closed permission policy applies
with scoped working files: the Agent may call granted MCP tools and read or
edit ordinary artifacts in its controller-created working directory. Ambient
files and terminals stay unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .catalog import BREAD_MCP_SERVER_NAME
from .output_continuation import ACP_TURN_LIMIT_REASONS
from .connectors.acp import (
    _ACPClientState,
    _ACP_TRANSPORT_SHUTDOWN_TIMEOUT,
    _acp_prompt_blocks,
    _acp_provider_request_failure,
    _cursor_question_as_elicitation,
    _enum_value,
    _field,
    _load_acp_sdk,
    _make_client,
    _opaque_activity_id,
    _managed_agent_process,
    _new_session,
    _permission_allowed,
    _permission_denied,
    _provider_incident_error,
    _provider_incident_from_exception,
    _record_policy_denial,
    _requires_explicit_authentication,
    _require_acp_attachment_capabilities,
    _replaylab_tool_for_permission,
    _stop_reason,
    _select_acp_model,
    _user_mcp_server_for_permission,
    _validate_scoped_permission_request,
    _wait_for,
)
from .connectors.acp_transport import ACP_MAX_MESSAGE_BYTES
from .conversation import ProviderBinaryAttachment
from .connectors.codex_app_server import _redact_timeline_text
from .errors import ConfigurationError, ConnectorError, PolicyError
from .agent_diagnostics import get_logger, public_error
from .owned_workspace import OwnedWorkspace

_ACP_TRANSPORT_MAX_BYTES = ACP_MAX_MESSAGE_BYTES
# ``session/new`` budget per attempt.  Vendors validate the stored login and
# start the declared ReplayLab MCP process inside this window; a healthy Cursor
# takes 5-8 s, so one transient stall (vendor backend, auto-update, network)
# earns exactly one fresh-process retry with a longer window before failing.
_ACP_SESSION_NEW_TIMEOUT = 60.0
_ACP_SESSION_NEW_RETRY_TIMEOUT = 90.0
_ACP_STARTUP_STAGES = ("ACP initialize", "ACP session/new")
_ACP_ROUTE_DIAGNOSIS_TIMEOUT = 20.0


def _identity_text(value: Any, *, limit: int = 128) -> str:
    """One bounded diagnostic field; never log the full initialize response."""

    return " ".join(_redact_timeline_text(value, limit=limit).split()) or "unknown"


@dataclass(slots=True)
class PromptResult:
    text: str
    segments: list[str]
    stop_reason: str
    tool_calls: int
    provider_incident: str = ""
    transport_retries: int = 0

    @property
    def final_text(self) -> str:
        # An empty tail after a tool call is significant: the provider never
        # supplied its conclusion. Earlier text was progress, not a final answer.
        return self.segments[-1] if self.segments else self.text


@dataclass
class ACPMainAgentSession:
    command: Sequence[str]
    cwd: Path
    env: Mapping[str, str] | None
    allowed_tools: frozenset[str]
    native_workspace_root: Path | None = None
    workspace_write: bool = False
    terminal_execution: bool = False
    # Native subscription mode supplies the task-scoped ReplayLab MCP filter
    # directly over stdio.
    mcp_stdio_command: str = ""
    mcp_stdio_args: Sequence[str] = ()
    mcp_stdio_env: Mapping[str, str] | None = None
    # MCP servers the user configured in Settings > AI. They sit next to the
    # task-scoped ReplayLab route; their tools are the user's own grant, never a
    # widening of the Host capture grant.
    extra_mcp_servers: Sequence[Mapping[str, Any]] = ()
    on_elicitation: Callable[[str, Any], Any] | None = None
    timeout: float = 86_400.0
    authentication_method: str = ""
    provider_id: str = ""
    model: str = ""
    stderr_path: Path | None = None
    # Vendor-specific ``session/new`` ``_meta``.
    session_meta: Mapping[str, Any] | None = None
    mcp_transport: str = field(default="", init=False)
    _sdk: Any = field(default=None, init=False, repr=False)
    _stack: contextlib.AsyncExitStack | None = field(default=None, init=False, repr=False)
    _connection: Any = field(default=None, init=False, repr=False)
    _session_id: str = field(default="", init=False)
    _stderr_handle: Any = field(default=None, init=False, repr=False)
    _state: _ACPClientState | None = field(default=None, init=False, repr=False)
    _initialized: Any = field(default=None, init=False, repr=False)
    _terminals: Any = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "ACPMainAgentSession":
        await self.open()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def open(self) -> None:
        if self._stack is not None:
            return
        if not self.command:
            raise ConfigurationError("ACP main Agent command is empty")
        budgets = (
            min(self.timeout, _ACP_SESSION_NEW_TIMEOUT),
            min(self.timeout, _ACP_SESSION_NEW_RETRY_TIMEOUT),
        )
        for attempt, budget in enumerate(budgets):
            try:
                await self._open_once(budget)
                return
            except ConnectorError as exc:
                stage = _startup_timeout_stage(exc)
                if stage is None:
                    raise
                if attempt + 1 < len(budgets):
                    get_logger().warning(
                        "%s timed out after %.0fs (%s); retrying once with a fresh Agent process",
                        stage,
                        budget,
                        Path(str(self.command[0])).name,
                    )
                    continue
                raise ConnectorError(
                    "%s timed out twice (%.0fs then %.0fs) for %s. %s"
                    % (
                        stage,
                        budgets[0],
                        budget,
                        Path(str(self.command[0])).name,
                        await self._diagnose_startup_timeout(),
                    )
                ) from exc

    async def _diagnose_startup_timeout(self) -> str:
        """Say which side stalled: the vendor process or the local Bread MCP route."""

        if not self.mcp_stdio_command or not self.allowed_tools:
            return (
                "No Bread MCP server was declared for this session, so the stall is "
                "inside the vendor Agent (login state, vendor backend, network, or an "
                "auto-update on first launch)."
            )
        try:
            verdict = await _diagnose_local_mcp_route(
                self.mcp_stdio_command,
                tuple(self.mcp_stdio_args),
                dict(self.mcp_stdio_env or {}),
                cwd=self.cwd,
                timeout=_ACP_ROUTE_DIAGNOSIS_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - diagnosis must never mask the timeout
            return "Local Bread MCP route could not be checked: %s" % public_error(exc)
        if verdict["ok"]:
            return (
                "The local Bread MCP route is healthy (initialize %.1fs, %d tools), so "
                "the stall is inside the vendor Agent: check its login state, backend "
                "reachability, or an auto-update on first launch, then retry."
                % (verdict["seconds"], verdict["tools"])
            )
        return (
            "The local Bread MCP route FAILED, which is what the vendor waited on: %s"
            % verdict["error"]
        )

    async def _open_once(self, session_new_timeout: float) -> None:
        self._sdk = sdk = _load_acp_sdk()
        self._state = state = _ACPClientState(
            on_delta=None,
            provider_id=self.provider_id,
            allowed_tools=frozenset(self.allowed_tools),
        )
        if self.terminal_execution:
            from .acp_terminal import ACPTerminals
            self._terminals = ACPTerminals(self.cwd, self.env or {})
        client = _main_agent_client(
            sdk,
            state,
            frozenset(self.allowed_tools),
            terminals=self._terminals,
            on_elicitation=self.on_elicitation,
            user_mcp_servers=frozenset(
                str(server.get("id", "")).strip()
                for server in self.extra_mcp_servers
                if str(server.get("id", "")).strip()
            ),
            native_workspace=(OwnedWorkspace(self.native_workspace_root, writable=self.workspace_write)
                              if self.native_workspace_root is not None else None),
        )
        stderr: Any = asyncio.subprocess.DEVNULL
        if self.stderr_path is not None:
            try:
                self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                self._stderr_handle = open(self.stderr_path, "ab")  # noqa: SIM115
                stderr = self._stderr_handle
            except OSError as exc:
                get_logger().warning("cannot capture ACP stderr: %s", public_error(exc))
        spawn_kwargs: dict[str, Any] = {"cwd": str(self.cwd)}
        if self.env is not None:
            spawn_kwargs["env"] = dict(self.env)
        stack = contextlib.AsyncExitStack()
        try:
            manager = sdk.spawn_agent_process(
                client,
                *self.command,
                transport_kwargs={
                    "stderr": stderr,
                    "limit": _ACP_TRANSPORT_MAX_BYTES,
                    "shutdown_timeout": _ACP_TRANSPORT_SHUTDOWN_TIMEOUT,
                },
                **spawn_kwargs,
            )
            connection, _process = await stack.enter_async_context(
                _managed_agent_process(manager, "ACP main Agent")
            )
            self._connection = connection
            initialize_arguments: dict[str, Any] = {
                "protocol_version": sdk.protocol_version
            }
            capabilities: dict[str, Any] = {}
            if self._terminals is not None:
                capabilities["terminal"] = True
            if self.native_workspace_root is not None:
                capabilities["fs"] = {"readTextFile": True, "writeTextFile": self.workspace_write}
            if self.on_elicitation is not None:
                capabilities["elicitation"] = {"form": {}}
            if capabilities:
                initialize_arguments["client_capabilities"] = capabilities
            initialized = await _wait_for(
                connection.initialize(**initialize_arguments),
                min(self.timeout, 30.0),
                "ACP initialize",
            )
            self._initialized = initialized
            if _requires_explicit_authentication(
                self.provider_id, self.authentication_method
            ):
                await _wait_for(
                    connection.authenticate(method_id=self.authentication_method),
                    min(self.timeout, 60.0),
                    "ACP authenticate",
                )
            mcp_servers = (
                [self._mcp_server_declaration(sdk, initialized)]
                if self.allowed_tools
                else []
            )
            mcp_servers.extend(self._user_mcp_server_declarations(sdk))
            session = await _new_session(
                connection,
                cwd=str(self.cwd),
                mcp_servers=mcp_servers,
                meta=self.session_meta,
                timeout=session_new_timeout,
            )
            session_id = str(_field(session, "session_id", "sessionId") or "")
            if not session_id:
                raise ConnectorError("ACP session/new returned no session id")
            self._session_id = state.session_id = session_id
            if self.model:
                await self._select_model(connection, session, session_id, self.model)
        except FileNotFoundError as exc:
            try:
                await stack.aclose()
            finally:
                self._connection = self._initialized = None
                self._session_id = ""
                self._close_stderr()
            raise ConnectorError(
                f"cannot start the ACP agent executable '{self.command[0]}'"
            ) from exc
        except BaseException as error:
            try:
                await stack.aclose()
            finally:
                self._connection = self._initialized = None
                self._session_id = ""
                self._close_stderr()
            failure = _acp_provider_request_failure(error, provider_id=self.provider_id)
            if failure is not None:
                raise failure from error
            raise
        self._stack = stack
        agent_info = _field(initialized, "agent_info", "agentInfo")
        get_logger().info(
            "ACP main Agent session %s opened (%s, agent=%s version=%s protocol=%s, host tools via %s MCP)",
            session_id,
            Path(str(self.command[0])).name,
            _identity_text(_field(agent_info, "name")),
            _identity_text(_field(agent_info, "version")),
            _identity_text(_field(initialized, "protocol_version", "protocolVersion"), limit=32),
            self.mcp_transport,
        )

    def _mcp_server_declaration(self, sdk: Any, initialized: Any) -> Any:
        """Declare the one task-scoped ReplayLab stdio MCP process directly."""

        del initialized
        if not self.mcp_stdio_command:
            raise ConfigurationError(
                "ACP Bread tools require a direct task-scoped stdio MCP route"
            )
        self.mcp_transport = "stdio"
        environment = [
            {"name": str(name), "value": str(value)}
            for name, value in (self.mcp_stdio_env or {}).items()
        ]
        arguments = {
            "name": BREAD_MCP_SERVER_NAME,
            "command": self.mcp_stdio_command,
            "args": list(self.mcp_stdio_args),
            "env": environment,
        }
        try:
            return sdk.mcp_server_stdio_type(**arguments)
        except Exception:
            return sdk.mcp_server_stdio_type(type="stdio", **arguments)

    def _user_mcp_server_declarations(self, sdk: Any) -> list[Any]:
        """Declare the user's own MCP servers; one bad entry never breaks the session."""

        declarations: list[Any] = []
        for server in self.extra_mcp_servers:
            identifier = str(server.get("id", "") or "").strip()
            if not identifier:
                continue
            try:
                if str(server.get("transport", "stdio")) == "stdio":
                    arguments = {
                        "name": identifier,
                        "command": str(server.get("command", "")),
                        "args": [str(value) for value in server.get("args", ()) or ()],
                        "env": [
                            {"name": str(name), "value": str(value)}
                            for name, value in (server.get("env") or {}).items()
                        ],
                    }
                    try:
                        declarations.append(sdk.mcp_server_stdio_type(**arguments))
                    except Exception:
                        declarations.append(sdk.mcp_server_stdio_type(type="stdio", **arguments))
                else:
                    http_type = getattr(sdk, "mcp_server_http_type", None)
                    if http_type is None:
                        get_logger().warning(
                            "MCP server %s skipped: this ACP SDK has no HTTP transport", identifier
                        )
                        continue
                    arguments = {"name": identifier, "url": str(server.get("url", ""))}
                    headers = _acp_http_headers(server)
                    if headers:
                        # Older SDK builds have no headers field; the server is still
                        # worth declaring without them.
                        try:
                            declarations.append(http_type(
                                headers=[{"name": name, "value": value}
                                         for name, value in headers.items()],
                                **arguments))
                            continue
                        except Exception:
                            get_logger().warning(
                                "MCP server %s declared without headers: this ACP SDK does not "
                                "accept them", identifier)
                    declarations.append(http_type(**arguments))
            except Exception as exc:  # noqa: BLE001 - a user server must not abort the task
                get_logger().warning(
                    "MCP server %s could not be declared: %s", identifier, public_error(exc)
                )
        return declarations

    async def close(self) -> None:
        terminals, self._terminals = self._terminals, None
        if terminals is not None:
            await terminals.close()
        stack, self._stack = self._stack, None
        connection, self._connection = self._connection, None
        self._initialized = None
        session_id, self._session_id = self._session_id, ""
        try:
            if connection is not None and session_id:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(connection.cancel(session_id=session_id), timeout=2.0)
        finally:
            try:
                if stack is not None:
                    # A surviving process is a failed cleanup, never a healthy
                    # closed session. The managed transport owns escalation.
                    await stack.aclose()
            finally:
                self._close_stderr()

    def _close_stderr(self) -> None:
        handle, self._stderr_handle = self._stderr_handle, None
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()

    async def _select_model(self, connection: Any, session: Any, session_id: str, model: str) -> None:
        await _select_acp_model(
            connection, session, session_id, model,
            timeout=self.timeout, provider_id=self.provider_id, env=self.env,
        )

    # ---------------------------------------------------------------- prompt
    async def compact_context(self) -> bool:
        """Use a provider-advertised command; never guess a vendor extension."""
        if self._state is None or "compact" not in self._state.available_commands:
            return False
        result = await self.prompt("/compact")
        # Maintenance output is not an assistant answer. A compaction command
        # which starts tools is not proof of a settled, reusable context.
        return result.stop_reason == "end_turn" and result.tool_calls == 0

    async def prompt(
        self,
        text: str,
        *,
        attachments: Sequence[ProviderBinaryAttachment] = (),
        on_delta: Callable[[str], Any] | None = None,
        on_timeline: Callable[[Mapping[str, Any]], Any] | None = None,
        on_reasoning: Callable[[Mapping[str, Any]], Any] | None = None,
        tool_boundary: Callable[[], None] | None = None,
    ) -> PromptResult:
        if self._stack is None or self._connection is None or self._state is None or self._sdk is None:
            raise ConnectorError("ACP main Agent session is not open")
        if not text.strip():
            raise ConfigurationError("ACP prompt must not be empty")
        _require_acp_attachment_capabilities(self._initialized, attachments)
        state = self._state
        segments: list[str] = [""]
        tool_calls = 0
        seen_tools: set[str] = set()

        async def delta(chunk: str) -> None:
            segments[-1] += chunk
            if on_delta is not None:
                result = on_delta(chunk)
                if inspect.isawaitable(result):
                    await result

        async def timeline(event: Mapping[str, Any]) -> None:
            nonlocal tool_calls
            activity_id = str(event.get("activityId") or "")
            kind = str(event.get("activityKind") or "")
            if (event.get("verification") != "localFileObserved" and activity_id
                    and activity_id not in seen_tools and "plan" not in kind.lower()):
                seen_tools.add(activity_id)
                tool_calls += 1
                if segments[-1]:
                    segments.append("")
                if tool_boundary is not None:
                    tool_boundary()
            if on_timeline is not None:
                result = on_timeline(event)
                if inspect.isawaitable(result):
                    await result

        # Reset per-prompt accumulators on the shared state object.
        state.on_delta = delta
        state.on_timeline = timeline
        state.on_reasoning = on_reasoning
        state.text_chunks = []
        state.text_chars = state.text_bytes = 0
        state.reasoning_chars = state.reasoning_bytes = 0
        state.reasoning_started = False
        state.reasoning_truncated = False
        state.policy_error = None
        state.callback_error = None
        state.provider_incident = None
        state.abort_event = asyncio.Event()
        state.timeline_events = 0

        connection = self._connection
        prompt_task = asyncio.create_task(
            _wait_for(
                connection.prompt(
                    session_id=self._session_id,
                    prompt=_acp_prompt_blocks(self._sdk, text, attachments),
                ),
                self.timeout,
                f"ACP model turn (configured task limit: {self.timeout:.0f}s)",
            )
        )
        abort_task = asyncio.create_task(state.abort_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {prompt_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if abort_task in done and prompt_task not in done:
                raise (
                    state.callback_error
                    or state.policy_error
                    or (
                        _provider_incident_error(state)
                        if state.provider_incident is not None
                        else None
                    )
                    or ConnectorError("ACP response collection was aborted")
                )
            response = await prompt_task
        except BaseException as error:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.cancel(session_id=self._session_id), timeout=2.0)
            failure = _acp_provider_request_failure(error, provider_id=self.provider_id)
            if failure is not None:
                raise failure from error
            incident = state.provider_incident or _provider_incident_from_exception(error)
            if incident is not None and not isinstance(error, asyncio.CancelledError):
                raise _provider_incident_error(state, incident) from None
            raise
        finally:
            for task in (prompt_task, abort_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(prompt_task, abort_task, return_exceptions=True)
        if state.policy_error is not None:
            raise state.policy_error
        if state.callback_error is not None:
            raise ConnectorError(f"ACP delta callback failed: {state.callback_error}") from state.callback_error
        if state.provider_incident is not None:
            raise _provider_incident_error(state)
        stop_reason = _stop_reason(response)
        if stop_reason not in {"end_turn", *ACP_TURN_LIMIT_REASONS}:
            raise ConnectorError(f"ACP Agent stopped without completing the turn: {stop_reason}")
        return PromptResult(
            text="".join(state.text_chunks),
            segments=[segment for segment in segments],
            stop_reason=stop_reason,
            tool_calls=tool_calls,
        )


def _main_agent_client(
    sdk: Any,
    state: _ACPClientState,
    allowed_tools: frozenset[str],
    *,
    on_elicitation: Callable[[str, Any], Any] | None = None,
    user_mcp_servers: frozenset[str] = frozenset(),
    native_workspace: OwnedWorkspace | None = None,
    terminals: Any = None,
) -> Any:
    """Grant authorized MCP calls and decline unavailable native calls individually.

    The main Agent has exactly one task-scoped ReplayLab MCP server
    and ambient file/terminal access is denied at the client level. Ordinary
    working artifacts may use the optional exact run-owned filesystem scope.
    A permission
    request is granted when it names one of the allowed tools in a recognised
    shape (``_replaylab_tool_for_permission``); anything else is denied without
    aborting the turn.
    """

    base_client = _make_client(sdk, state)
    base_type = type(base_client)

    class MainAgentClient(base_type):  # type: ignore[misc, valid-type]
        def _terminal(self, session_id: str) -> Any:
            if terminals is None or not state.session_id or session_id != state.session_id:
                raise PolicyError("Terminal capability is not enabled for this ACP session")
            return terminals

        async def create_terminal(self, session_id: str, command: str, args=None, env=None, cwd=None, output_byte_limit=None, **kwargs: Any) -> Any:
            return await self._terminal(session_id).create(command, args, cwd, env, output_byte_limit)

        async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
            return await self._terminal(session_id).output(terminal_id)

        async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
            return await self._terminal(session_id).wait(terminal_id)

        async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
            return await self._terminal(session_id).kill(terminal_id)

        async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
            return await self._terminal(session_id).release(terminal_id)

        async def request_permission(
            self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any
        ) -> Any:
            del kwargs
            validation_error = _validate_scoped_permission_request(state, session_id, tool_call, options)
            if validation_error:
                return await super().request_permission(session_id, tool_call, options)
            state.permission_ids.discard(str(_field(tool_call, "tool_call_id", "toolCallId") or "").strip())
            if (
                _replaylab_tool_for_permission(tool_call, allowed_tools) is not None
                or _user_mcp_server_for_permission(tool_call, user_mcp_servers) is not None
                or _owned_file_permission(tool_call, native_workspace)
            ):
                for wanted in ("allow_once", "allow_always"):
                    for option in options:
                        kind = _enum_value(_field(option, "kind")).replace("-", "_").lower()
                        if kind == wanted:
                            state.permission_ids.add(
                                str(_field(tool_call, "tool_call_id", "toolCallId") or "").strip()
                            )
                            return _permission_allowed(sdk, option)
                return _permission_denied(sdk)
            # This is a vendor generic tool, not a missing Host effect.  Keep the
            # two permission systems distinct and fail closed here rather than
            # falling through to an ambient vendor/user policy.
            _record_policy_denial(
                state, "use a vendor generic tool outside the Host grant"
            )
            return _permission_denied(sdk)

        async def read_text_file(self, session_id: str, path: str,
                                 line: int | None = None, limit: int | None = None, **kwargs: Any) -> Any:
            del kwargs
            if native_workspace is None or not state.session_id or session_id != state.session_id:
                return await super().read_text_file(session_id, path, line=line, limit=limit)
            observation: dict[str, Any] | None = {} if state.on_timeline is not None else None
            content = await asyncio.to_thread(native_workspace.read, path, line, limit,
                                             observation=observation)
            if observation:
                await _observe_working_file(state, observation)
            return {"content": content}

        async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: Any) -> Any:
            del kwargs
            if native_workspace is None or not state.session_id or session_id != state.session_id:
                return await super().write_text_file(session_id, path, content)
            observation: dict[str, Any] | None = {} if state.on_timeline is not None else None
            await asyncio.to_thread(native_workspace.write, path, content, observation=observation)
            if observation:
                await _observe_working_file(state, observation)
            return {}

        async def create_elicitation(
            self, message: str, mode: Any, **kwargs: Any
        ) -> Any:
            del kwargs
            if on_elicitation is not None:
                try:
                    result = on_elicitation(message, mode)
                    if inspect.isawaitable(result):
                        await result
                except Exception as error:
                    state.callback_error = error
                    state.abort_event.set()
            response_type = sdk.decline_elicitation_response_type
            if response_type is not None:
                try:
                    return response_type(action="decline")
                except Exception:
                    pass
            return {"action": "decline"}

        async def ext_method(
            self, method: str, params: dict[str, Any]
        ) -> dict[str, Any]:
            if state.provider_id == "cursor-acp" and method == "cursor/ask_question":
                projected = _cursor_question_as_elicitation(params)
                if projected is not None and on_elicitation is not None:
                    try:
                        result = on_elicitation(*projected)
                        if inspect.isawaitable(result):
                            await result
                    except Exception as error:
                        state.callback_error = error
                        state.abort_event.set()
            # Cursor must receive a response to unblock its turn. The native
            # ReplayLab task ends as needs-user-input and carries the answer in
            # the next self-contained turn, so this particular ACP request is
            # intentionally marked skipped rather than answered speculatively.
            return await super().ext_method(method, params)

    return MainAgentClient()


async def _observe_working_file(state: _ACPClientState, observation: Mapping[str, Any]) -> None:
    """Best-effort local fs fact; never attribute a native command or replay."""
    callback = state.on_timeline
    if callback is None:
        return
    try:
        unused_key, activity_id = _opaque_activity_id(
            state.working_file_activity_ids, uuid.uuid4().hex, "file")
        if not activity_id:
            return
        target = "fs/read_text_file" if observation["action"] == "read" else "fs/write_text_file"
        event = {"activityId": activity_id, "activityKind": "toolCall",
                 "activityTarget": target, "toolKind": "read" if observation["action"] == "read" else "edit",
                 "status": "completed", "lifecycle": "completed",
                 "verification": "localFileObserved", "hostVerified": False,
                 "workingFileObservation": dict(observation)}
        result = callback(event)
        if inspect.isawaitable(result):
            await result
    except Exception:
        # Observability cannot revoke an already successful read/write or
        # turn a full task into failure. No path/content is logged here.
        with contextlib.suppress(Exception):
            get_logger().warning("Could not publish the owned working-file observation")


def _owned_file_permission(tool_call: Any, workspace: OwnedWorkspace | None) -> bool:
    if workspace is None:
        return False
    kind = _enum_value(_field(tool_call, "kind")).replace("-", "_").lower()
    if kind not in {"read", "edit"}:
        return False
    raw = _field(tool_call, "raw_input", "rawInput")
    paths = []
    if isinstance(raw, Mapping):
        paths.extend(raw[key] for key in ("path", "file_path", "filePath", "target_file", "targetFile", "absolute_path", "absolutePath")
                     if isinstance(raw.get(key), str))
    locations = _field(tool_call, "locations")
    if isinstance(locations, (list, tuple)):
        paths.extend(value for item in locations if isinstance((value := _field(item, "path")), str))
    if not paths:
        return False
    try:
        for path in paths:
            workspace.path(path, write=kind == "edit")
    except Exception:
        return False
    return True



def _startup_timeout_stage(exc: BaseException) -> str | None:
    message = str(exc)
    for stage in _ACP_STARTUP_STAGES:
        if message.startswith(stage) and "timed out" in message:
            return stage
    return None


def _acp_http_headers(server: Mapping[str, Any]) -> dict[str, str]:
    """Headers for a streamable-HTTP MCP server, with the token read from the environment."""

    import os

    headers = {str(k): str(v) for k, v in (server.get("headers") or {}).items()}
    for name, variable in (server.get("headerEnv") or {}).items():
        value = os.environ.get(str(variable), "")
        if value:
            headers[str(name)] = value
    bearer = os.environ.get(str(server.get("bearerEnv") or ""), "")
    if bearer:
        headers["Authorization"] = "Bearer %s" % bearer
    return headers


async def _diagnose_local_mcp_route(
    command: str,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    *,
    cwd: Path,
    timeout: float,
) -> dict[str, Any]:
    """Run the declared ReplayLab MCP command once and report initialize/list_tools."""

    import os
    import time

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = dict(os.environ)
    env.update({str(key): str(value) for key, value in environment.items()})
    parameters = StdioServerParameters(
        command=str(command), args=[str(item) for item in arguments], env=env, cwd=str(cwd)
    )
    started = time.monotonic()
    try:
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout)
                listing = await asyncio.wait_for(session.list_tools(), timeout=timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        detail = _flatten_exception(exc)
        return {"ok": False, "seconds": time.monotonic() - started, "tools": 0, "error": detail}
    tools = len(list(getattr(listing, "tools", ()) or ()))
    return {"ok": True, "seconds": time.monotonic() - started, "tools": tools, "error": ""}


def _flatten_exception(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", None)
    if nested:
        return "; ".join(_flatten_exception(item) for item in nested)
    text = public_error(exc) if not isinstance(exc, asyncio.TimeoutError) else "timed out"
    return "%s: %s" % (type(exc).__name__, text) if text else type(exc).__name__

__all__ = ["ACPMainAgentSession", "CLAUDE_BUILTIN_TOOLS", "PromptResult", "write_claude_settings"]
