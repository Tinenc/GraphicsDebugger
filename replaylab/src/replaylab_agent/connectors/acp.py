"""Official Agent Client Protocol adapter for external agent CLIs."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes as _ctypes
import inspect
import json
import os
import subprocess
import re
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..agent_diagnostics import get_logger
from ..catalog import BREAD_MCP_SERVER_NAME
from ..conversation import (
    MAX_CONVERSATION_ATTACHMENT_TOTAL_BINARY_BYTES,
    MAX_CONVERSATION_ATTACHMENTS_PER_TURN,
    ProviderBinaryAttachment,
)
from ..errors import (
    AuthenticationError,
    ConfigurationError,
    ConnectorError,
    LifecycleCleanupError,
    PolicyError,
    ProviderIncidentError,
    ProviderCapabilityError,
    ProviderRequestError,
)
from .codex_app_server import (
    DeltaCallback,
    ExternalAgentResult,
    ReasoningCallback,
    TimelineCallback,
    checked_response_size,
)
from .acp_transport import ACP_MAX_MESSAGE_BYTES, spawn_bounded_agent_process


# Give a well-behaved Agent a short graceful window. The ACP SDK transport must
# wait longer than ReplayLab's tree escalation, otherwise its Windows
# ``Process.terminate()`` kills only a cmd/PowerShell launcher and can orphan the
# real Agent. After the tree stop, keep a separate bounded window for the SDK to
# finish its pipe and receive-task cleanup.
_ACP_CLEANUP_TIMEOUT = 2.0
_ACP_TREE_KILL_TIMEOUT = 5.0
_ACP_FORCE_CLEANUP_TIMEOUT = 5.0
_ACP_TRANSPORT_SHUTDOWN_TIMEOUT = 15.0
_ACP_FINAL_PIPE_DRAIN_TIMEOUT = 0.5
_ACP_TRANSPORT_MAX_BYTES = ACP_MAX_MESSAGE_BYTES
_ACP_STDERR_TAIL_BYTES = 4000
_CURSOR_PROVIDER_ID = "cursor-acp"
_ACP_PROVIDER_ERROR_UPDATE_KINDS = frozenset({
    "agent_error",
    "agent_error_chunk",
    "error",
    "error_chunk",
    "provider_error",
    "provider_error_chunk",
})


@dataclass(frozen=True, slots=True)
class _ProviderIncident:
    code: str
    message: str


def _classify_provider_incident(
    value: Any, *, explicit_error: bool = False
) -> _ProviderIncident | None:
    """Recognise transport diagnostics that must never become answer text.

    Ordinary Shader/compiler diagnostics can legitimately begin with ``Error:``.
    Text chunks are therefore classified only when they also contain a narrow
    provider/transport signature.  A protocol-level ACP error update is already
    typed by the vendor and can use the generic fallback.
    """

    text = str(value or "").replace("\0", " ").strip()
    if not text:
        return None
    folded = " ".join(text.casefold().split())
    http2 = "http/2" in folded or "http2" in folded
    cancelled = "cancel" in folded or "canceled" in folded
    if http2 and cancelled and (
        "stream closed" in folded
        or "error code" in folded
        or "retriableerror" in folded
    ):
        return _ProviderIncident(
            "http2-cancel",
            "The model provider closed the HTTP/2 stream before the turn completed.",
        )
    if "ping" in folded and (
        "timeout" in folded
        or "timed out" in folded
        or "deadline" in folded
    ):
        return _ProviderIncident(
            "provider-ping-timeout",
            "The model provider stopped answering transport keep-alive checks.",
        )
    if "retriableerror" in folded and any(
        marker in folded
        for marker in ("cancel", "timeout", "timed out", "connection", "stream")
    ):
        return _ProviderIncident(
            "provider-retriable-error",
            "The model provider reported a retryable transport failure.",
        )
    looks_like_transport_error = any(
        marker in folded
        for marker in (
            "provider error",
            "transport error",
            "connection closed",
            "connection reset",
            "network error",
            "stream closed",
        )
    )
    if looks_like_transport_error and (
        explicit_error
        or folded.startswith("error:")
        or folded.startswith("api error:")
    ):
        return _ProviderIncident(
            "provider-transport-error",
            "The model provider reported a transport failure.",
        )
    if explicit_error:
        return _ProviderIncident(
            "provider-error-update",
            "The model provider ended the turn with an error notification.",
        )
    return None


def _split_provider_incident(text: str) -> tuple[str, _ProviderIncident | None]:
    """Keep valid assistant prose preceding a standalone provider error line."""

    offset = 0
    for line in str(text or "").splitlines(keepends=True):
        incident = _classify_provider_incident(line)
        if incident is not None:
            return text[:offset], incident
        offset += len(line)
    incident = _classify_provider_incident(text)
    return ("", incident) if incident is not None else (text, None)


def _acp_context_request_failure(error: BaseException) -> ProviderRequestError | None:
    """Accept explicit context errors from RPC failures, never assistant text."""
    try:
        from acp.exceptions import RequestError
    except ImportError:
        return None
    current, seen = error, set()
    for _ in range(6):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        if isinstance(current, RequestError) and current.code in {-32603, -32000}:
            data = current.data if isinstance(current.data, Mapping) else {}
            nested = data.get("error") if isinstance(data.get("error"), Mapping) else {}
            rendered = str(current)[:4096]
            http = re.search(r"\bAPI Error:\s*([45][0-9]{2})\b", rendered)
            status = int(http.group(1)) if http else nested.get("status", data.get("status"))
            if status is not None and status not in {400, 413, 422}:
                return None
            kinds = {str(data.get(key) or "").lower() for key in ("errorKind", "code")}
            kinds.add(str(nested.get("code") or "").lower())
            explicit = bool(kinds & {"context_length_exceeded", "context_window_exceeded",
                                    "context_limit_exceeded", "prompt_too_long"})
            if not explicit and http:
                from ..context_management import is_context_size_error
                proxy_error = RuntimeError(rendered)
                proxy_error.status_code = status
                explicit = is_context_size_error(proxy_error)
            if explicit:
                return ProviderRequestError("The provider rejected input exceeding its context window.",
                                            error_code="provider-context")
        current = current.__cause__ or current.__context__
    return None


def _acp_provider_request_failure(
    error: BaseException, *, provider_id: str,
) -> ProviderRequestError | None:
    """Preserve context failures and Claude's typed errors before wrapping.

    The adapter forwards SDKAssistantMessage.error as RequestError.data.errorKind.
    Older releases only embed a vendor HTTP failure in that same RPC error.
    Assistant chunks, tool results and stderr are deliberately never inspected.
    """
    if isinstance(error, ProviderRequestError):
        return None
    context_failure = _acp_context_request_failure(error)
    if context_failure is not None:
        return context_failure
    if provider_id != "claude-code-acp":
        return None
    try:
        from acp.exceptions import RequestError
    except ImportError:
        return None
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(6):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        if isinstance(current, RequestError) and current.code == -32603:
            break
        current = current.__cause__ or current.__context__
    else:
        return None
    data = current.data if isinstance(current.data, Mapping) else {}
    kind = data.get("errorKind")
    known = {
        "authentication_failed": "provider-authentication",
        "oauth_org_not_allowed": "provider-permission",
        "account_on_hold": "provider-quota", "billing_error": "provider-quota",
        "rate_limit": "provider-rate-limit", "overloaded": "provider-capacity",
        "invalid_request": "provider-configuration", "model_not_found": "provider-model-unavailable",
        "max_output_tokens": "provider-context", "server_error": "provider-unavailable",
        "transport_lost": "provider-transport",
    }
    code = known.get(kind, "") if isinstance(kind, str) else ""
    http = re.match(
        r"\A(?:Internal error:\s*)?(?:Failed to authenticate\.\s*)?API Error:\s*([45][0-9]{2})(?=\s|$)",
        str(current)[:4096],
    )
    if http is not None:
        status = int(http.group(1))
        # A concrete 403 is an access denial, even when the vendor's wrapper
        # loosely calls every credential-related HTTP failure authentication.
        if status == 401:
            code = "provider-authentication"
        elif status == 403:
            code = "provider-permission"
        elif status == 402:
            code = "provider-quota"
        elif status == 429 and code != "provider-quota":
            code = "provider-rate-limit"
        elif not code:
            if status in {400, 404, 422}:
                code = "provider-configuration"
            elif status in {408, 504}:
                code = "provider-timeout"
            elif 500 <= status <= 599:
                code = "provider-unavailable"
    if not code:
        return None
    from ..agent_diagnostics import public_error
    return ProviderRequestError(
        "ACP provider request failed: " + public_error(current), error_code=code,
        retryable=code in {"provider-rate-limit", "provider-capacity", "provider-unavailable",
                          "provider-timeout", "provider-transport"},
    )


@dataclass(frozen=True, slots=True)
class _WindowsJobApi:
    create_job: Any
    set_information: Any
    open_process: Any
    assign_process: Any
    terminate_job: Any
    close_handle: Any


_WINDOWS_JOB_API_LOCK = threading.Lock()
_WINDOWS_JOB_API: _WindowsJobApi | None = None
if os.name == "nt":
    from ctypes import wintypes as _wintypes


class _WindowsJobIoCounters(_ctypes.Structure):
    _fields_ = [
        (name, _ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _WindowsJobBasicLimits(_ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _ctypes.c_longlong),
        ("PerJobUserTimeLimit", _ctypes.c_longlong),
        ("LimitFlags", _ctypes.c_uint32),
        ("MinimumWorkingSetSize", _ctypes.c_size_t),
        ("MaximumWorkingSetSize", _ctypes.c_size_t),
        ("ActiveProcessLimit", _ctypes.c_uint32),
        ("Affinity", _ctypes.c_size_t),
        ("PriorityClass", _ctypes.c_uint32),
        ("SchedulingClass", _ctypes.c_uint32),
    ]


class _WindowsJobExtendedLimits(_ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WindowsJobBasicLimits),
        ("IoInfo", _WindowsJobIoCounters),
        ("ProcessMemoryLimit", _ctypes.c_size_t),
        ("JobMemoryLimit", _ctypes.c_size_t),
        ("PeakProcessMemoryUsed", _ctypes.c_size_t),
        ("PeakJobMemoryUsed", _ctypes.c_size_t),
    ]


def _windows_job_api() -> _WindowsJobApi | None:
    """Return one pointer-width-safe WinAPI binding for all ACP processes."""

    global _WINDOWS_JOB_API
    if os.name != "nt":
        return None
    if _WINDOWS_JOB_API is not None:
        return _WINDOWS_JOB_API
    with _WINDOWS_JOB_API_LOCK:
        if _WINDOWS_JOB_API is not None:
            return _WINDOWS_JOB_API
        kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = [_ctypes.c_void_p, _wintypes.LPCWSTR]
        create_job.restype = _wintypes.HANDLE
        set_information = kernel32.SetInformationJobObject
        set_information.argtypes = [
            _wintypes.HANDLE,
            _ctypes.c_int,
            _ctypes.c_void_p,
            _wintypes.DWORD,
        ]
        set_information.restype = _wintypes.BOOL
        open_process = kernel32.OpenProcess
        open_process.argtypes = [_wintypes.DWORD, _wintypes.BOOL, _wintypes.DWORD]
        open_process.restype = _wintypes.HANDLE
        assign_process = kernel32.AssignProcessToJobObject
        assign_process.argtypes = [_wintypes.HANDLE, _wintypes.HANDLE]
        assign_process.restype = _wintypes.BOOL
        terminate_job = kernel32.TerminateJobObject
        terminate_job.argtypes = [_wintypes.HANDLE, _wintypes.UINT]
        terminate_job.restype = _wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [_wintypes.HANDLE]
        close_handle.restype = _wintypes.BOOL
        _WINDOWS_JOB_API = _WindowsJobApi(
            create_job=create_job,
            set_information=set_information,
            open_process=open_process,
            assign_process=assign_process,
            terminate_job=terminate_job,
            close_handle=close_handle,
        )
        return _WINDOWS_JOB_API


def _requires_explicit_authentication(provider_id: str, method_id: str) -> bool:
    """Return whether the ACP connection must invoke ``authenticate``.

    Cursor officially supports pre-authentication through ``agent login`` (the
    ReplayLab connection wizard uses that path).  Current Cursor ACP builds can
    leave a redundant ``authenticate(cursor_login)`` request pending even when
    the managed profile is already signed in, while ``session/new`` immediately
    validates and accepts the same stored login.  Keep the catalogued method for
    UI/provider metadata, but do not re-enter login on every probe or task.

    Other ACP adapters still receive their declared authentication method.
    """

    return bool(str(method_id or "").strip()) and str(provider_id or "").strip() != _CURSOR_PROVIDER_ID


def _read_capture_tail(capture: Any, limit: int = _ACP_STDERR_TAIL_BYTES) -> str:
    """Return a bounded tail of a captured stderr temp file for diagnostics.

    The ACP connection test spawns the vendor Agent with its stderr redirected to
    a private temporary file so a launch/handshake failure (a missing runtime, a
    crash on startup, an authentication error printed by the CLI) can be shown to
    the user instead of the bare "Connection closed" the transport surfaces. The
    tail is size-capped and blank-line stripped; it never contains credentials the
    Agent would only emit over its protocol channel, not stderr.
    """

    if capture is None:
        return ""
    try:
        capture.flush()
    except Exception:
        pass
    try:
        capture.seek(0, os.SEEK_END)
        capture.seek(max(0, capture.tell() - limit))
        data = capture.read(limit)
    except Exception:
        return ""
    if not data:
        return ""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    if len(data) > limit:
        data = data[-limit:]
    text = data.decode("utf-8", "replace")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-20:])
_MAX_ACP_TIMELINE_ACTIVITIES = 128
_MAX_ACP_TIMELINE_EVENTS = 512
_MAX_ACP_ARGUMENT_NAMES = 16
_MAX_ACP_ARGUMENT_NAME_CHARS = 64


@dataclass(slots=True)
class _ACPSDK:
    protocol_version: Any
    spawn_agent_process: Callable[..., Any]
    text_block: Callable[[str], Any]
    image_block: Callable[..., Any] | None
    embedded_blob_resource: Callable[..., Any] | None
    resource_block: Callable[[Any], Any] | None
    client_type: type[Any]
    mcp_server_stdio_type: type[Any]
    mcp_server_http_type: type[Any]
    http_header_type: type[Any]
    request_permission_response_type: type[Any] | None
    allowed_outcome_type: type[Any] | None
    denied_outcome_type: type[Any] | None
    decline_elicitation_response_type: type[Any] | None


def _load_acp_sdk() -> _ACPSDK:
    """Import the optional ACP SDK only when an ACP profile is selected."""

    try:
        import acp
        from acp import schema
    except ImportError as exc:
        raise ConnectorError(
            "ACP support is not installed; install Bread with the 'acp' extra "
            "(agent-client-protocol>=0.12,<1)"
        ) from exc
    try:
        from acp.interfaces import Client
    except ImportError:
        Client = getattr(acp, "Client", None)
    required = {
        "PROTOCOL_VERSION": getattr(acp, "PROTOCOL_VERSION", None),
        "spawn_agent_process": getattr(acp, "spawn_agent_process", None),
        "text_block": getattr(acp, "text_block", None),
        "Client": Client,
        "McpServerStdio": getattr(schema, "McpServerStdio", None),
        # Current ACP session/new uses the discriminated HttpMcpServer union
        # member. Older SDKs exposed the same transport as McpServerHttp.
        "HttpMcpServer": (
            getattr(schema, "HttpMcpServer", None)
            or getattr(schema, "McpServerHttp", None)
        ),
        "HttpHeader": getattr(schema, "HttpHeader", None),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ConnectorError(
            "installed Agent Client Protocol SDK is incompatible; missing: "
            + ", ".join(missing)
            + ". Install a supported agent-client-protocol>=0.12,<1 release."
        )
    return _ACPSDK(
        protocol_version=required["PROTOCOL_VERSION"],
        spawn_agent_process=spawn_bounded_agent_process,
        text_block=required["text_block"],
        image_block=getattr(acp, "image_block", None),
        embedded_blob_resource=getattr(acp, "embedded_blob_resource", None),
        resource_block=getattr(acp, "resource_block", None),
        client_type=required["Client"],
        mcp_server_stdio_type=required["McpServerStdio"],
        mcp_server_http_type=required["HttpMcpServer"],
        http_header_type=required["HttpHeader"],
        request_permission_response_type=getattr(schema, "RequestPermissionResponse", None),
        allowed_outcome_type=(
            getattr(schema, "AllowedOutcome", None)
            or getattr(schema, "SelectedPermissionOutcome", None)
        ),
        denied_outcome_type=getattr(schema, "DeniedOutcome", None),
        decline_elicitation_response_type=getattr(schema, "DeclineElicitationResponse", None),
    )


def _acp_attachment_uri(attachment: ProviderBinaryAttachment) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", attachment.title).strip("._")
    safe_name = (safe_name or ("image" if attachment.kind == "image" else "file"))[:96]
    return "attachment://" + quote(attachment.id, safe="._-") + "/" + quote(
        safe_name, safe="._-"
    )


def _acp_prompt_blocks(
    sdk: _ACPSDK,
    prompt: str,
    attachments: Sequence[ProviderBinaryAttachment],
) -> list[Any]:
    values = tuple(attachments)
    if len(values) > MAX_CONVERSATION_ATTACHMENTS_PER_TURN:
        raise ConfigurationError("ACP attachment count exceeds the per-turn limit")
    if sum(len(attachment.data) for attachment in values) > (
        MAX_CONVERSATION_ATTACHMENT_TOTAL_BINARY_BYTES
    ):
        raise ConfigurationError("ACP attachments exceed the 8 MiB per-turn limit")
    blocks: list[Any] = [sdk.text_block(prompt)]
    for attachment in values:
        encoded = base64.b64encode(attachment.data).decode("ascii")
        uri = _acp_attachment_uri(attachment)
        if attachment.kind == "image":
            if sdk.image_block is None:
                raise ConnectorError(
                    "installed ACP SDK cannot transport image attachments; update "
                    "agent-client-protocol"
                )
            blocks.append(
                sdk.image_block(
                    data=encoded,
                    mime_type=attachment.media_type,
                    uri=uri,
                )
            )
            continue
        if sdk.embedded_blob_resource is None or sdk.resource_block is None:
            raise ConnectorError(
                "installed ACP SDK cannot transport file attachments; update "
                "agent-client-protocol"
            )
        resource = sdk.embedded_blob_resource(
            uri=uri,
            blob=encoded,
            mime_type=attachment.media_type,
        )
        blocks.append(sdk.resource_block(resource))
    return blocks


def _require_acp_attachment_capabilities(
    initialized: Any,
    attachments: Sequence[ProviderBinaryAttachment],
) -> None:
    values = tuple(attachments)
    if not values:
        return
    capabilities = getattr(initialized, "agent_capabilities", None)
    prompt_capabilities = getattr(capabilities, "prompt_capabilities", None)
    if any(attachment.kind == "image" for attachment in values) and (
        getattr(prompt_capabilities, "image", False) is not True
    ):
        raise ConnectorError(
            "the selected ACP Agent does not advertise image prompt support"
        )
    if any(attachment.kind == "file" for attachment in values) and (
        getattr(prompt_capabilities, "embedded_context", False) is not True
    ):
        raise ConnectorError(
            "the selected ACP Agent does not advertise embedded file context support"
        )


def _owned_agent_process(value: Any) -> Any | None:
    if not isinstance(value, (tuple, list)) or len(value) < 2:
        return None
    process = value[1]
    pid = getattr(process, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return process


def _discard_cleanup_task(task: asyncio.Future[Any]) -> None:
    task.cancel()

    def consume(done: asyncio.Future[Any]) -> None:
        with contextlib.suppress(BaseException):
            done.exception()

    task.add_done_callback(consume)


async def _finalize_agent_process_transport(process: Any, label: str) -> None:
    """Reap the SDK-owned process and close its asyncio pipe transports.

    Python 3.10's Windows subprocess ``wait()`` completes when the process
    exits, before Proactor pipe ``connection_lost`` callbacks necessarily run.
    The ACP SDK closes stdin and waits for the process, but a short-lived CLI
    may close the event loop while stdout and the parent subprocess transport
    are still awaiting finalization. Drain any final protocol bytes briefly,
    then explicitly close the transport while its event loop is still alive.
    """

    stdin = getattr(process, "stdin", None)
    if stdin is not None:
        close_stdin = getattr(stdin, "close", None)
        if callable(close_stdin):
            close_stdin()
        wait_closed = getattr(stdin, "wait_closed", None)
        if callable(wait_closed):
            closed = wait_closed()
            if inspect.isawaitable(closed):
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await closed

    wait = getattr(process, "wait", None)
    if callable(wait):
        waited = wait()
        if not inspect.isawaitable(waited):
            raise LifecycleCleanupError(f"{label} process wait is not awaitable")
        await asyncio.wait_for(waited, timeout=_ACP_FORCE_CLEANUP_TIMEOUT)

    try:
        communicate = getattr(process, "communicate", None)
        if callable(communicate):
            drained = communicate()
            if inspect.isawaitable(drained):
                with contextlib.suppress(
                    asyncio.TimeoutError,
                    BrokenPipeError,
                    ConnectionResetError,
                    OSError,
                ):
                    await asyncio.wait_for(
                        drained, timeout=_ACP_FINAL_PIPE_DRAIN_TIMEOUT
                    )
    finally:
        # asyncio.subprocess.Process has no public close method. The transport
        # is the owner of all Proactor pipe handles, and close() is idempotent.
        transport = getattr(process, "_transport", None)
        close_transport = getattr(transport, "close", None)
        if callable(close_transport):
            close_transport()
            # Let queued Proactor connection_lost callbacks run before an
            # enclosing asyncio.run() closes the event loop.
            await asyncio.sleep(0)


async def _force_stop_agent_process_tree(process: Any) -> bool:
    """Stop the exact owned Agent process tree without a shell.

    ``asyncio.subprocess.Process.kill`` is only ``TerminateProcess`` on Windows.
    Cursor's official launcher is cmd -> PowerShell -> node, so root-only
    termination is not proof that the ACP Agent was reclaimed. Use the absolute
    system taskkill executable for that bounded Windows escalation, then retain
    a root-process fallback for platforms without process-tree support here.
    """

    pid = getattr(process, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if getattr(process, "returncode", None) is not None:
        # A completed Windows launcher is not proof that an already detached
        # PowerShell/node descendant exited. Without a live root PID taskkill
        # cannot verify the tree, so retain fail-closed semantics there.
        return os.name != "nt"

    tree_stop_confirmed = os.name != "nt"
    if os.name == "nt":
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        taskkill = system_root / "System32" / "taskkill.exe"
        if taskkill.is_file():
            killer: asyncio.subprocess.Process | None = None
            try:
                killer = await asyncio.create_subprocess_exec(
                    str(taskkill),
                    "/PID",
                    str(pid),
                    "/T",
                    "/F",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return_code = await asyncio.wait_for(
                    killer.wait(), timeout=_ACP_TREE_KILL_TIMEOUT
                )
                tree_stop_confirmed = return_code == 0
            except (OSError, asyncio.TimeoutError):
                if killer is not None and killer.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        killer.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(killer.wait(), timeout=1.0)

    if getattr(process, "returncode", None) is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    return tree_stop_confirmed


class _WindowsJob:
    """Kill-on-close job object holding one vendor launcher and its descendants.

    Cursor's official launcher is ``cmd -> PowerShell -> node``.  Closing the
    ACP transport ends ``cmd`` but the node process (and the MCP proxy it
    spawned) can outlive it, still holding the per-profile Cursor state; the
    next ``session/new`` then blocks on that lock until the orphan dies, which
    is exactly the "timed out after 60 s, fresh process opened in 7 s" pattern
    seen in the field.  A job object with ``KILL_ON_JOB_CLOSE`` makes the
    whole tree terminate deterministically when the session closes.
    """

    _LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    def __init__(self) -> None:
        self.handle: Any = None
        self.assigned = False
        self._api: _WindowsJobApi | None = None

    @classmethod
    def assign(cls, pid: Any) -> "_WindowsJob | None":
        if os.name != "nt" or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        api = _windows_job_api()
        if api is None:
            return None
        return cls._assign_with_api(pid, api)

    @classmethod
    def _assign_with_api(
        cls, pid: int, api: _WindowsJobApi
    ) -> "_WindowsJob | None":
        """Assign one PID while retaining full pointer-width HANDLE values."""

        job: _WindowsJob | None = None
        process_handle: Any = None
        try:
            job = cls()
            job._api = api
            job.handle = api.create_job(None, None)
            if not job.handle:
                return None
            limits = _WindowsJobExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = cls._LIMIT_KILL_ON_JOB_CLOSE
            if not api.set_information(
                job.handle, cls._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                _ctypes.byref(limits), _ctypes.sizeof(limits)):
                job.close(terminate=False)
                return None
            process_handle = api.open_process(
                cls._PROCESS_SET_QUOTA | cls._PROCESS_TERMINATE, False, int(pid))
            if not process_handle:
                job.close(terminate=False)
                return None
            try:
                job.assigned = bool(api.assign_process(job.handle, process_handle))
            finally:
                api.close_handle(process_handle)
                process_handle = None
            if not job.assigned:
                job.close(terminate=False)
                return None
            return job
        except Exception:  # noqa: BLE001 - the job is a safety net, never a failure
            if process_handle:
                try:
                    api.close_handle(process_handle)
                except Exception:
                    pass
            if job is not None:
                job.close(terminate=False)
            return None

    def close(self, *, terminate: bool = True) -> None:
        handle, self.handle = self.handle, None
        if not handle:
            return
        api, self._api = self._api or _windows_job_api(), None
        if api is None:
            return
        try:
            if terminate and self.assigned:
                api.terminate_job(handle, 0)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                api.close_handle(handle)
            except Exception:  # noqa: BLE001
                pass


@contextlib.asynccontextmanager
async def _managed_agent_process(manager: Any, label: str):
    """Preserve body failures while making an uncertain child exit fatal."""

    value = await manager.__aenter__()
    process = _owned_agent_process(value)
    job = _WindowsJob.assign(getattr(process, "pid", None)) if process is not None else None

    async def close(
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: Any,
    ) -> bool | None:
        try:
            awaitable = manager.__aexit__(exception_type, exception, traceback)
            if not inspect.isawaitable(awaitable):
                raise TypeError("ACP process manager __aexit__ is not awaitable")
            cleanup_task = asyncio.ensure_future(awaitable)
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(cleanup_task), timeout=_ACP_CLEANUP_TIMEOUT
                )
                if process is not None:
                    await _finalize_agent_process_transport(process, label)
                return result
            except (asyncio.TimeoutError, asyncio.CancelledError) as cleanup_error:
                cancellation_error = (
                    cleanup_error
                    if isinstance(cleanup_error, asyncio.CancelledError)
                    else None
                )
                if process is None:
                    _discard_cleanup_task(cleanup_task)
                    raise LifecycleCleanupError(
                        f"{label} process cleanup was not confirmed within the deadline"
                    ) from cleanup_error
                force_task = asyncio.create_task(
                    _force_stop_agent_process_tree(process)
                )
                try:
                    tree_stop_confirmed = await asyncio.shield(force_task)
                except asyncio.CancelledError as tree_cancelled:
                    # Do not let a second user cancellation interrupt the exact
                    # tree stop. Preserve it and propagate only after both the
                    # process and SDK transport have reached a terminal state.
                    cancellation_error = tree_cancelled
                    try:
                        tree_stop_confirmed = await asyncio.wait_for(
                            asyncio.shield(force_task),
                            timeout=_ACP_TREE_KILL_TIMEOUT + 1.0,
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError) as tree_error:
                        _discard_cleanup_task(force_task)
                        _discard_cleanup_task(cleanup_task)
                        raise LifecycleCleanupError(
                            f"{label} process tree cleanup was not confirmed"
                        ) from tree_error
                except Exception as tree_error:
                    _discard_cleanup_task(cleanup_task)
                    raise LifecycleCleanupError(
                        f"{label} process tree cleanup failed"
                    ) from tree_error
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(cleanup_task),
                        timeout=_ACP_FORCE_CLEANUP_TIMEOUT,
                    )
                except asyncio.CancelledError as forced_cancelled:
                    cancellation_error = forced_cancelled
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(cleanup_task),
                            timeout=_ACP_FORCE_CLEANUP_TIMEOUT,
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError) as forced_error:
                        _discard_cleanup_task(cleanup_task)
                        raise LifecycleCleanupError(
                            f"{label} process cleanup was not confirmed within the deadline"
                        ) from forced_error
                except asyncio.TimeoutError as forced_error:
                    _discard_cleanup_task(cleanup_task)
                    raise LifecycleCleanupError(
                        f"{label} process cleanup was not confirmed within the deadline"
                    ) from forced_error
                await _finalize_agent_process_transport(process, label)
                if os.name == "nt" and not tree_stop_confirmed:
                    raise LifecycleCleanupError(
                        f"{label} process tree cleanup was not confirmed"
                    ) from cleanup_error
                if cancellation_error is not None:
                    raise cancellation_error
                return result
        except LifecycleCleanupError:
            raise
        except Exception as cleanup_error:
            raise LifecycleCleanupError(
                f"{label} process cleanup failed"
            ) from cleanup_error

    try:
        yield value
    except BaseException as primary:
        try:
            suppressed = await close(type(primary), primary, primary.__traceback__)
        finally:
            if job is not None:
                job.close()
        if suppressed:
            return
        raise
    else:
        try:
            await close(None, None, None)
        finally:
            # Graceful shutdown first; then the job guarantees no detached
            # launcher descendant survives to block the next session.
            if job is not None:
                job.close()



def _acp_selected_model(session: Any) -> str:
    """The model id the agent reports for this session, if it reports one at all.

    `session/new` is authoritative about the model the vendor selected. Agents that
    expose no model option (the Claude Code adapter, for one) report nothing, and the
    caller has to say so rather than imply the requested model was used.
    """

    for option in _field(session, "config_options", "configOptions") or []:
        category = _enum_value(_field(option, "category")).lower()
        option_id = str(_field(option, "id") or "")
        if category == "model" or option_id.lower() in {"model", "models"}:
            return str(_field(option, "current_value", "currentValue") or "")
    return str(_field(_field(session, "models"), "current_model_id", "currentModelId") or "")


async def _select_acp_model(
    connection: Any, session: Any, session_id: str, model: str, *,
    timeout: float, provider_id: str = "", env: Mapping[str, str] | None = None,
) -> str:
    """Bind an explicitly requested model before any prompt, without fallback."""

    if not model:
        return _acp_selected_model(session)
    options = _field(session, "config_options", "configOptions") or []
    model_option = next((option for option in options if (
        _enum_value(_field(option, "category")).lower() == "model"
        or str(_field(option, "id") or "").lower() in {"model", "models"}
    )), None)
    if _acp_selected_model(session) == model:
        return model
    if model_option is None:
        # The first-party Claude adapter uses this documented environment
        # channel. Other Agents must not silently run their default instead.
        if (provider_id == "claude-code-acp"
                and (env or {}).get("ANTHROPIC_MODEL") == model.strip().lower()):
            return model
        raise ProviderCapabilityError(
            "The ACP Agent does not expose a supported way to select the requested model.",
            error_code="model-selection-unsupported", capability="model-selection",
        )
    setter = getattr(connection, "set_config_option", None)
    if not callable(setter):
        raise ProviderCapabilityError(
            "ACP agent advertised model selection, but the connection does not support session/set_config_option",
            error_code="model-selection-unsupported", capability="model-selection",
        )
    try:
        selected = await _wait_for(
            setter(config_id=str(_field(model_option, "id") or ""),
                   session_id=session_id, value=model),
            min(timeout, 30.0), "ACP model selection",
        )
    except AuthenticationError:
        raise
    except Exception as exc:
        from ..agent_diagnostics import public_error
        raise ConnectorError(
            f"ACP model selection for '{model}' failed: {public_error(exc)}"
        ) from exc
    reported = _acp_selected_model(selected)
    if reported and reported != model:
        # claude-agent-acp 0.76 session-model.js exposes picker aliases while
        # setSessionConfigOption resolves a requested full ID against its private
        # ModelInfo.resolvedModel, calls query.setModel, then returns that alias.
        # Accept the adapter's successful selection, not a client-side family /
        # version guess. Requiring the explicit setter also preserves a failed
        # PreModelSwitch hook instead of trusting the environment alone.
        if (provider_id == "claude-code-acp"
                and (env or {}).get("ANTHROPIC_MODEL") == model.strip().lower()
                and reported in _connection_models(selected)
                and reported in _connection_models(session)):
            return reported
        raise ConnectorError("ACP Agent did not select the requested model: " + model)
    return model


class ACPConnector:
    """Bounded ACP connection and model diagnostics.

    Real ReplayLab tasks use :class:`ACPMainAgentSession` so the vendor owns
    the reasoning loop and receives the task-scoped MCP server directly.  This
    connector deliberately has no workflow, proxy, receipt, or audit state.
    """

    def __init__(
        self,
        command: Sequence[str] | str,
        *,
        cwd: Path | str | None = None,
        timeout: float = 300.0,
        env: Mapping[str, str] | None = None,
        authentication_method: str = "",
        provider_id: str = "",
        session_meta: Mapping[str, Any] | None = None,
        stderr_path: Path | str | None = None,
    ) -> None:
        if isinstance(command, str):
            command = (command,)
        self.command = tuple(str(value) for value in command)
        if (
            not self.command
            or not self.command[0]
            or any("\0" in value for value in self.command)
        ):
            raise ConfigurationError("ACP agent command must be a non-empty argv sequence")
        self.cwd = Path(cwd or Path.cwd()).resolve()
        if not self.cwd.is_dir():
            raise ConfigurationError(f"ACP working directory does not exist: {self.cwd}")
        if timeout <= 0:
            raise ConfigurationError("ACP timeout must be positive")
        self.timeout = float(timeout)
        self.env = dict(env) if env is not None else None
        self.authentication_method = str(authentication_method or "").strip()
        if len(self.authentication_method) > 128 or "\0" in self.authentication_method:
            raise ConfigurationError("ACP authentication method is invalid")
        self.provider_id = str(provider_id or "").strip()
        if len(self.provider_id) > 128 or "\0" in self.provider_id:
            raise ConfigurationError("ACP provider id is invalid")
        self.session_meta = dict(session_meta) if session_meta else None
        self.stderr_path = Path(stderr_path).resolve() if stderr_path else None
        self._run_lock = asyncio.Lock()

    async def _authenticate(self, connection: Any) -> None:
        if not _requires_explicit_authentication(
            self.provider_id, self.authentication_method
        ):
            return
        await _wait_for(
            connection.authenticate(method_id=self.authentication_method),
            min(self.timeout, 60.0),
            "ACP authenticate",
        )

    async def test_connection(self) -> dict[str, Any]:
        """Perform a no-prompt ACP initialize/session handshake.

        This deliberately stops before ``session/prompt`` so the Settings
        connection test cannot consume model quota or grant capture tools.
        Creating a session is still required: several vendor Agents validate
        their saved account only at ``session/new``.
        """

        async with self._run_lock:
            sdk = _load_acp_sdk()
            state = _ACPClientState(
                on_delta=None,
                provider_id=self.provider_id,
            )
            client = _make_client(sdk, state)
            spawn_kwargs: dict[str, Any] = {"cwd": str(self.cwd)}
            if self.env is not None:
                spawn_kwargs["env"] = self.env
            # Capture the vendor Agent's stderr to a private temp file so a launch
            # or handshake failure reports the CLI's real error instead of a bare
            # "Connection closed" from the transport. The file is read only on the
            # failure paths below and always closed in the finally block.
            stderr_capture: Any = None
            with contextlib.suppress(Exception):
                stderr_capture = tempfile.TemporaryFile()
            try:
                manager = sdk.spawn_agent_process(
                    client,
                    *self.command,
                    transport_kwargs={
                        "stderr": stderr_capture
                        if stderr_capture is not None
                        else asyncio.subprocess.DEVNULL,
                        "limit": _ACP_TRANSPORT_MAX_BYTES,
                        "shutdown_timeout": _ACP_TRANSPORT_SHUTDOWN_TIMEOUT,
                    },
                    **spawn_kwargs,
                )
                async with _managed_agent_process(manager, "ACP connection test") as (
                    connection,
                    _process,
                ):
                    initialized = await _wait_for(
                        connection.initialize(protocol_version=sdk.protocol_version),
                        min(self.timeout, 30.0),
                        "ACP initialize",
                    )
                    await self._authenticate(connection)
                    session = await _new_session(
                        connection, cwd=str(self.cwd), mcp_servers=[],
                        meta=self.session_meta, timeout=min(self.timeout, 60.0),
                    )
                    session_id = str(_field(session, "session_id", "sessionId") or "")
                    if not session_id:
                        raise ConnectorError("ACP session/new returned no session id")
                    state.session_id = session_id
                    if state.policy_error is not None:
                        raise state.policy_error
                    return {
                        "sessionId": session_id,
                        "models": _connection_models(session),
                        "agentInfo": _wire_agent_info(initialized),
                    }
            except (AuthenticationError, ConfigurationError, ConnectorError, PolicyError) as exc:
                failure = _acp_provider_request_failure(exc, provider_id=self.provider_id)
                if failure is not None:
                    raise failure from exc
                raise
            except FileNotFoundError as exc:
                raise ConnectorError(
                    "cannot start ACP agent; verify the configured executable and "
                    "connection settings"
                ) from exc
            except TypeError as exc:
                raise ConnectorError(
                    "installed Agent Client Protocol SDK has an incompatible handshake API"
                ) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = _acp_provider_request_failure(exc, provider_id=self.provider_id)
                if failure is not None:
                    raise failure from exc
                detail = str(exc) or type(exc).__name__
                stderr_tail = _read_capture_tail(stderr_capture)
                if stderr_tail:
                    detail = f"{detail}; agent stderr: {stderr_tail}"
                raise ConnectorError(f"ACP connection test failed: {detail}") from exc
            finally:
                if stderr_capture is not None:
                    with contextlib.suppress(Exception):
                        stderr_capture.close()

    async def test_model(
        self, *, prompt: str = "Reply with exactly: OK", model: str = ""
    ) -> dict[str, Any]:
        """Send one minimal prompt to confirm the configured model actually generates.

        Unlike ``test_connection`` this DOES spend a small amount of provider
        quota: it runs a single short turn with no ReplayLab tools, so an invalid
        or plan-restricted model id surfaces here instead of on the user's first
        real prompt. The requested model uses the same exact session selection
        as a real task, or the Claude adapter's explicit environment binding.
        Returns ``{"ok": True, "stopReason": ...}`` on a completed text response and
        raises ConnectorError (with the Agent's stderr tail) otherwise.
        """

        async with self._run_lock:
            sdk = _load_acp_sdk()
            state = _ACPClientState(
                on_delta=None,
                provider_id=self.provider_id,
            )
            client = _make_client(sdk, state)
            spawn_kwargs: dict[str, Any] = {"cwd": str(self.cwd)}
            if self.env is not None:
                spawn_kwargs["env"] = self.env
            stderr_capture: Any = None
            with contextlib.suppress(Exception):
                stderr_capture = tempfile.TemporaryFile()
            try:
                manager = sdk.spawn_agent_process(
                    client,
                    *self.command,
                    transport_kwargs={
                        "stderr": stderr_capture
                        if stderr_capture is not None
                        else asyncio.subprocess.DEVNULL,
                        "limit": _ACP_TRANSPORT_MAX_BYTES,
                        "shutdown_timeout": _ACP_TRANSPORT_SHUTDOWN_TIMEOUT,
                    },
                    **spawn_kwargs,
                )
                async with _managed_agent_process(manager, "ACP model test") as (
                    connection,
                    _process,
                ):
                    await _wait_for(
                        connection.initialize(protocol_version=sdk.protocol_version),
                        min(self.timeout, 30.0),
                        "ACP initialize",
                    )
                    await self._authenticate(connection)
                    session = await _new_session(
                        connection, cwd=str(self.cwd), mcp_servers=[],
                        meta=self.session_meta, timeout=min(self.timeout, 60.0),
                    )
                    session_id = str(_field(session, "session_id", "sessionId") or "")
                    if not session_id:
                        raise ConnectorError("ACP session/new returned no session id")
                    state.session_id = session_id
                    if state.policy_error is not None:
                        raise state.policy_error
                    wanted = str(model or "").strip()
                    selected = await _select_acp_model(
                        connection, session, session_id, wanted,
                        timeout=self.timeout, provider_id=self.provider_id, env=self.env,
                    )
                    prompt_response = await _wait_for(
                        connection.prompt(
                            session_id=session_id,
                            prompt=_acp_prompt_blocks(sdk, prompt, ()),
                        ),
                        self.timeout,
                        "ACP session/prompt",
                    )
                    if state.provider_incident is not None:
                        raise _provider_incident_error(state)
                    if state.policy_error is not None:
                        raise state.policy_error
                    if state.callback_error is not None:
                        raise ConnectorError("ACP model response could not be collected") from state.callback_error
                    stop_reason = _stop_reason(prompt_response)
                    if stop_reason != "end_turn" or not "".join(state.text_chunks).strip():
                        raise ConnectorError(
                            "ACP model test did not produce a completed text response: " + stop_reason
                        )
                    result = {"ok": True, "stopReason": stop_reason}
                    if selected:
                        result["model"] = selected
                        if wanted and selected != wanted:
                            result["requestedModel"] = wanted
                    return result
            except (AuthenticationError, ConfigurationError, ConnectorError, PolicyError) as exc:
                failure = _acp_provider_request_failure(exc, provider_id=self.provider_id)
                if failure is not None:
                    raise failure from exc
                raise
            except FileNotFoundError as exc:
                raise ConnectorError(
                    "cannot start ACP agent; verify the configured executable and "
                    "connection settings"
                ) from exc
            except TypeError as exc:
                raise ConnectorError(
                    "installed Agent Client Protocol SDK has an incompatible handshake API"
                ) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = _acp_provider_request_failure(exc, provider_id=self.provider_id)
                if failure is not None:
                    raise failure from exc
                detail = str(exc) or type(exc).__name__
                stderr_tail = _read_capture_tail(stderr_capture)
                if stderr_tail:
                    detail = f"{detail}; agent stderr: {stderr_tail}"
                raise ConnectorError(f"ACP model test failed: {detail}") from exc
            finally:
                if stderr_capture is not None:
                    with contextlib.suppress(Exception):
                        stderr_capture.close()



@dataclass(slots=True)
class _ACPClientState:
    on_delta: DeltaCallback | None
    on_timeline: TimelineCallback | None = None
    on_reasoning: ReasoningCallback | None = None
    provider_id: str = ""
    allowed_tools: frozenset[str] = frozenset()
    session_id: str = ""
    permission_denials: int = 0
    permission_ids: set[str] = field(default_factory=set)
    text_chunks: list[str] = field(default_factory=list)
    text_chars: int = 0
    text_bytes: int = 0
    reasoning_chars: int = 0
    reasoning_bytes: int = 0
    reasoning_started: bool = False
    reasoning_truncated: bool = False
    policy_error: PolicyError | None = None
    callback_error: Exception | None = None
    provider_incident: _ProviderIncident | None = None
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    timeline_events: int = 0
    tool_activity_ids: dict[str, str] = field(default_factory=dict)
    tool_statuses: dict[str, str] = field(default_factory=dict)
    tool_kinds: dict[str, str] = field(default_factory=dict)
    tool_targets: dict[str, str] = field(default_factory=dict)
    native_tool_targets: dict[str, str] = field(default_factory=dict)
    working_file_activity_ids: dict[str, str] = field(default_factory=dict)
    tool_argument_names: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tool_input_identities: dict[str, str] = field(default_factory=dict)
    plan_activity_ids: dict[str, str] = field(default_factory=dict)
    plan_statuses: dict[str, str] = field(default_factory=dict)
    available_commands: frozenset[str] = frozenset()


def _provider_incident_from_exception(
    error: BaseException,
) -> _ProviderIncident | None:
    if isinstance(error, ProviderIncidentError):
        return _ProviderIncident(error.incident_code, str(error))
    pending: BaseException | None = error
    seen: set[int] = set()
    parts: list[str] = []
    while pending is not None and id(pending) not in seen and len(parts) < 4:
        seen.add(id(pending))
        parts.append(str(pending))
        pending = pending.__cause__ or pending.__context__
    rendered = " ".join(parts)
    incident = _classify_provider_incident(rendered)
    if incident is not None:
        return incident
    if "ACP session/prompt timed out" in rendered:
        return _ProviderIncident(
            "provider-turn-timeout",
            "The model provider did not complete the turn before its transport deadline.",
        )
    return None


def _provider_incident_error(
    state: _ACPClientState,
    incident: _ProviderIncident | None = None,
) -> ProviderIncidentError:
    selected = incident or state.provider_incident or _ProviderIncident(
        "provider-transport-error",
        "The model provider interrupted the turn before completion.",
    )
    in_flight: list[str] = []
    for key, status in state.tool_statuses.items():
        if status in {"completed", "failed", "cancelled"}:
            continue
        target = state.tool_targets.get(key) or state.tool_kinds.get(key) or "tool-call"
        in_flight.append(target)
    return ProviderIncidentError(
        selected.message,
        incident_code=selected.code,
        partial_text="".join(state.text_chunks),
        in_flight_tools=tuple(in_flight),
    )


async def _forward_reasoning(
    state: _ACPClientState,
    *,
    text: str = "",
    lifecycle: str = "updated",
    availability: str = "available",
) -> None:
    callback = state.on_reasoning
    if callback is None or state.callback_error is not None:
        return
    item_id = f"acp-thought:{state.session_id or 'pending'}"
    event = {
        "source": "acp",
        "channel": "raw",
        "itemId": item_id,
        "reasoningId": json.dumps(
            [item_id, "raw", 0], ensure_ascii=False, separators=(",", ":")
        ),
        "contentIndex": 0,
        "contentFormat": "text/plain",
        "text": text,
        "reset": False,
        "lifecycle": lifecycle,
        "availability": availability,
    }
    try:
        result = callback(event)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        state.callback_error = exc
        state.abort_event.set()


def _make_client(sdk: _ACPSDK, state: _ACPClientState) -> Any:
    class ReplayLabACPClient(sdk.client_type):  # type: ignore[misc, valid-type]
        def on_connect(self, conn: Any) -> None:
            self.connection = conn

        async def request_permission(
            self,
            session_id: str,
            tool_call: Any,
            options: list[Any],
            **kwargs: Any,
        ) -> Any:
            del kwargs
            validation_error = _validate_scoped_permission_request(
                state, session_id, tool_call, options
            )
            if validation_error:
                state.policy_error = PolicyError(
                    "ACP agent sent an invalid permission request; Bread cancelled it "
                    f"(fail-closed: {validation_error})"
                )
                state.abort_event.set()
                return _permission_denied(sdk)

            allowed_option = _scoped_replaylab_permission_option(
                state, tool_call, options
            )
            if allowed_option is not None:
                # This is not ambient Agent authority. The exact server/tool is
                # already in this fresh session's Host grant, and the direct
                # MCP filter enforces the allow-list independently. Prefer
                # allow_once; ACP allow_always is session-scoped.
                return _permission_allowed(sdk, allowed_option)

            state.permission_denials += 1
            # The protocol's denial carries no message, so the *reason* has to be
            # visible somewhere else: the log names the label that did not resolve.
            # A vendor's relabelled tool must not turn ordinary denials into a
            # task-wide cancellation; other granted calls remain available.
            denied_label = str(_field(tool_call, "title") or _field(tool_call, "name")
                               or _field(tool_call, "kind") or "")[:160]
            get_logger().warning(
                "ACP permission denied (%d): %r is not a granted Bread tool label; "
                "other authorized calls remain available",
                state.permission_denials, denied_label)
            # Every ACP provider expects a permission response. An otherwise
            # well-formed request outside the task grant is denied without
            # aborting the connector, so the child can continue through an
            # admitted ReplayLab route or return a text-only observation.
            return _permission_denied(sdk)

        async def create_elicitation(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            _record_policy_denial(state, "request interactive input")
            response_type = sdk.decline_elicitation_response_type
            if response_type is not None:
                try:
                    return response_type(action="decline")
                except Exception:
                    pass
            return {"action": "decline"}

        async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
            del elicitation_id, kwargs
            _record_policy_denial(state, "complete elicitation")

        async def read_text_file(
            self,
            session_id: str,
            path: str,
            line: int | None = None,
            limit: int | None = None,
            **kwargs: Any,
        ) -> Any:
            del session_id, path, line, limit, kwargs
            raise _policy_denial(state, "read a local file")

        async def write_text_file(
            self, session_id: str, path: str, content: str, **kwargs: Any
        ) -> Any:
            del session_id, path, content, kwargs
            raise _policy_denial(state, "write a local file")

        async def create_terminal(
            self,
            session_id: str,
            command: str,
            args: list[str] | None = None,
            env: list[Any] | None = None,
            cwd: str | None = None,
            output_byte_limit: int | None = None,
            **kwargs: Any,
        ) -> Any:
            del session_id, command, args, env, cwd, output_byte_limit, kwargs
            raise _policy_denial(state, "create a terminal")

        async def terminal_output(
            self, session_id: str, terminal_id: str, **kwargs: Any
        ) -> Any:
            del session_id, terminal_id, kwargs
            raise _policy_denial(state, "read terminal output")

        async def release_terminal(
            self, session_id: str, terminal_id: str, **kwargs: Any
        ) -> Any:
            del session_id, terminal_id, kwargs
            raise _policy_denial(state, "release a terminal")

        async def wait_for_terminal_exit(
            self, session_id: str, terminal_id: str, **kwargs: Any
        ) -> Any:
            del session_id, terminal_id, kwargs
            raise _policy_denial(state, "wait for a terminal")

        async def kill_terminal(
            self, session_id: str, terminal_id: str, **kwargs: Any
        ) -> Any:
            del session_id, terminal_id, kwargs
            raise _policy_denial(state, "kill a terminal")

        async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            del params
            if state.provider_id == _CURSOR_PROVIDER_ID:
                if method == "cursor/ask_question":
                    return {
                        "outcome": {
                            "outcome": "skipped",
                            "reason": "Bread does not provide interactive answers during this automated turn.",
                        }
                    }
                if method == "cursor/create_plan":
                    return {
                        "outcome": {
                            # A plan is the provider's proposed reasoning, not an
                            # effect grant. Accepting it lets Cursor continue the
                            # user's turn; each later tool call still goes through
                            # the task-scoped MCP and permission boundaries.
                            "outcome": "accepted",
                        }
                    }
            # An unsupported extension is not an effect grant or a protocol
            # violation. Let the agent handle the standard JSON-RPC rejection
            # and continue using the capabilities this client does implement.
            from acp.exceptions import RequestError

            raise RequestError.method_not_found(method)

        async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
            # Follow ACP's forward-compatible custom notification contract:
            # https://agentclientprotocol.com/protocol/v1/extensibility
            # Ignore unhandled notifications from every provider, including
            # CodeBuddy's codebuddy.ai/command. Never execute their payloads,
            # project them as completion evidence, or grant any capability.
            del method, params

        async def session_update(
            self,
            session_id: str,
            update: Any,
            **kwargs: Any,
        ) -> None:
            del kwargs
            if state.session_id and session_id != state.session_id:
                return
            update_kind = _update_kind(update)
            if update_kind == "available_commands_update":
                commands = _field(update, "available_commands", "availableCommands")
                if isinstance(commands, (list, tuple)):
                    state.available_commands = frozenset(
                        name for item in commands[:128]
                        if isinstance(name := _field(item, "name"), str) and 0 < len(name) <= 128
                    )
                return
            if update_kind in _ACP_PROVIDER_ERROR_UPDATE_KINDS:
                incident = _classify_provider_incident(
                    _extract_provider_error_text(update), explicit_error=True
                )
                if incident is not None and state.provider_incident is None:
                    state.provider_incident = incident
                state.abort_event.set()
                return
            if update_kind == "agent_thought_chunk":
                text = _extract_text(update)
                if not text or state.callback_error is not None or state.reasoning_truncated:
                    return
                try:
                    state.reasoning_chars, state.reasoning_bytes = checked_response_size(
                        text, state.reasoning_chars, state.reasoning_bytes
                    )
                except Exception:
                    # Raw-trace limits are observational and must not abort an
                    # otherwise valid provider turn. The runtime owns the
                    # authoritative task-wide truncation marker.
                    state.reasoning_truncated = True
                    await _forward_reasoning(
                        state,
                        lifecycle="truncated",
                        availability="limit-reached",
                    )
                    return
                lifecycle = "started" if not state.reasoning_started else "updated"
                state.reasoning_started = True
                await _forward_reasoning(state, text=text, lifecycle=lifecycle)
                return

            if update_kind in {
                "plan",
                "plan_update",
                "plan_removed",
                "tool_call",
                "tool_call_update",
            }:
                await _handle_public_timeline_update(state, update, update_kind)
                return

            if not _is_agent_message(update):
                return
            text = _extract_text(update)
            if not text or state.callback_error is not None:
                return
            text, incident = _split_provider_incident(text)
            if incident is not None:
                if state.provider_incident is None:
                    state.provider_incident = incident
                state.abort_event.set()
            if not text:
                return
            try:
                state.text_chars, state.text_bytes = checked_response_size(
                    text, state.text_chars, state.text_bytes
                )
            except Exception as exc:
                state.callback_error = exc
                state.abort_event.set()
                return
            state.text_chunks.append(text)
            callback = state.on_delta
            if callback is None or state.callback_error is not None:
                return
            try:
                result = callback(text)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                # Keep the protocol notification handler responsive, then surface
                # the host callback failure by cancelling the active prompt.
                state.callback_error = exc
                state.abort_event.set()

    return ReplayLabACPClient()


def _cursor_question_as_elicitation(
    params: Mapping[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Project Cursor's first blocking question into ACP form-elicitation shape."""

    questions = params.get("questions")
    if not isinstance(questions, list):
        return None
    for raw_question in questions:
        if not isinstance(raw_question, Mapping):
            continue
        prompt = str(raw_question.get("prompt") or "").strip()[:2400]
        options = raw_question.get("options")
        if not prompt or not isinstance(options, list):
            continue
        variants: list[dict[str, str]] = []
        for raw_option in options[:6]:
            if not isinstance(raw_option, Mapping):
                continue
            option_id = str(raw_option.get("id") or "").strip()[:64]
            label = str(raw_option.get("label") or "").strip()[:240]
            if option_id and label:
                variants.append({"const": option_id, "title": label})
        if not variants:
            continue
        property_id = str(raw_question.get("id") or "question").strip()[:64]
        property_schema: dict[str, Any] = {"description": prompt}
        if raw_question.get("allowMultiple") is True:
            property_schema.update(
                {
                    "type": "array",
                    "items": {"anyOf": variants},
                }
            )
        else:
            property_schema["oneOf"] = variants
        title = str(params.get("title") or prompt).strip()[:2400]
        return (
            title,
            {
                "requestedSchema": {
                    "type": "object",
                    "properties": {property_id or "question": property_schema},
                }
            },
        )
    return None


def _policy_denial(state: _ACPClientState, action: str) -> PolicyError:
    error = PolicyError(
        f"ACP Agent attempted to {action}; this ACP client did not authorize "
        "that native operation."
    )
    _record_policy_denial(state, action)
    if state.policy_error is not None:
        return state.policy_error
    return error


def _record_policy_denial(state: _ACPClientState, action: str) -> None:
    """Record a denied ordinary call without cancelling the Agent's task.

    Malformed scoped permission requests have their own terminal handler.
    Repeated attempts at an unavailable native tool do not revoke unrelated,
    already authorized Bread operations.
    """

    state.permission_denials += 1
    get_logger().debug("ACP native call denied (%d): %s", state.permission_denials, action)


def _permission_denied(sdk: _ACPSDK) -> Any:
    response_type = sdk.request_permission_response_type
    outcome_type = sdk.denied_outcome_type
    if response_type is not None and outcome_type is not None:
        try:
            return response_type(outcome=outcome_type(outcome="cancelled"))
        except Exception:
            pass
    # The official quickstart also permits this mapping form.
    return {"outcome": {"outcome": "cancelled"}}


def _permission_allowed(sdk: _ACPSDK, option: Any) -> Any:
    option_id = str(_field(option, "option_id", "optionId") or "").strip()
    response_type = sdk.request_permission_response_type
    outcome_type = sdk.allowed_outcome_type
    if response_type is not None and outcome_type is not None:
        for kwargs in (
            {"optionId": option_id, "outcome": "selected"},
            {"option_id": option_id, "outcome": "selected"},
            {"optionId": option_id},
            {"option_id": option_id},
        ):
            try:
                return response_type(outcome=outcome_type(**kwargs))
            except Exception:
                continue
    # The ACP protocol also permits the equivalent mapping form.
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


async def _new_session(
    connection: Any,
    *,
    cwd: str,
    mcp_servers: list[Any],
    meta: Mapping[str, Any] | None,
    timeout: float,
) -> Any:
    """``session/new`` with an optional vendor ``_meta`` block.

    Current SDKs serialize their **kwargs directly under wire ``_meta``.
    Older explicit parameter spellings are selected locally from the signature.
    Never resend session/new because a provider returned a validation error:
    the first request may already have created a session and started its MCPs.
    """

    extra: dict[str, Any] = {}
    if meta:
        payload = dict(meta)
        try:
            parameters = inspect.signature(connection.new_session).parameters
        except (TypeError, ValueError) as exc:
            raise ConnectorError("cannot determine the ACP session metadata interface") from exc
        for name in ("field_meta", "meta", "_meta"):
            parameter = parameters.get(name)
            if parameter is not None and parameter.kind != inspect.Parameter.VAR_KEYWORD:
                extra = {name: payload}
                break
        else:
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                extra = payload
            else:
                raise ProviderCapabilityError(
                    "The ACP SDK cannot pass the required session metadata.",
                    error_code="session-metadata-unsupported", capability="session-metadata",
                )
    return await _wait_for(
        connection.new_session(cwd=cwd, mcp_servers=mcp_servers, **extra),
        timeout, "ACP session/new",
    )


# Tool-call kinds that can never be a ReplayLab MCP call: shell commands and
# file mutations are the agent's own built-in tools.
_NON_MCP_TOOL_KINDS = frozenset({"execute", "edit", "delete", "move"})


def _tool_from_label(label: Any, allowed_tools: frozenset[str]) -> str | None:
    """Map a permission request's ``name``/``title`` onto one allowed tool.

    Only exact, vendor-known spellings match: ``mcp__Bread__<tool>``
    (Claude Code), ``<tool> (Bread MCP Server)`` (Gemini CLI family),
    ``Bread__<tool>`` / ``Bread:<tool>`` and the bare tool name. No substring
    matching: a shell command that merely mentions a tool name does not qualify.
    """

    if type(label) is not str:
        return None
    text = label.strip().casefold()
    if not text or len(text) > 512:
        return None
    server = BREAD_MCP_SERVER_NAME.casefold()
    for tool in allowed_tools:
        lowered = tool.casefold()
        if text in (
            lowered,
            f"mcp__{server}__{lowered}",
            f"{server}__{lowered}",
            f"{server}_{lowered}",
            f"{server}:{lowered}",
            f"{server}: {lowered}",
            f"{server}/{lowered}",
            f"{server}.{lowered}",
            f"{server} - {lowered}",
            f"{lowered} ({server} mcp server)",
            f"{lowered} ({server})",
        ):
            return tool
    return None


def _user_mcp_server_for_permission(tool_call: Any, servers: frozenset[str]) -> str | None:
    """Return the user-configured MCP server a permission request names, else ``None``.

    Servers added in Settings > AI are the user's own grant. Their tools are
    approved the same way the ReplayLab route's are, and only while the request
    names one of the servers the user enabled for this session; file, terminal
    and other non-MCP tool kinds stay denied.
    """

    if not servers:
        return None
    raw_input = _field(tool_call, "raw_input", "rawInput")
    if isinstance(raw_input, Mapping):
        server = raw_input.get("server")
        if type(server) is str:
            return server if server in servers else None
    kind = _enum_value(_field(tool_call, "kind")).replace("-", "_").lower()
    if kind in _NON_MCP_TOOL_KINDS:
        return None
    for label in (
        _field(tool_call, "name", "tool_name", "toolName"),
        _field(tool_call, "title"),
    ):
        text = str(label or "").strip()
        if not text:
            continue
        for server in servers:
            if text.startswith("mcp__%s__" % server) or text.endswith("(%s)" % server):
                return server
    return None


def _replaylab_tool_for_permission(tool_call: Any, allowed_tools: frozenset[str]) -> str | None:
    """Return the allowed ReplayLab tool a permission request names, else ``None``.

    Two shapes are recognised.  Structured: ``rawInput.server`` +
    ``rawInput.tool`` (Zed-style adapters).  Labelled: ``rawInput`` carries the
    tool *arguments* and the tool is identified by ``name``/``title`` (Claude
    Code's ``claude-agent-acp`` sends ``mcp__ReplayLab__<tool>``, Gemini CLI
    sends ``<tool> (ReplayLab MCP Server)``).  The label path is safe because the
    only MCP endpoint the agent can reach is the task-scoped filter, which
    enforces the allow-list and the capture scope on every call regardless of
    what was approved here.
    """

    raw_input = _field(tool_call, "raw_input", "rawInput")
    if isinstance(raw_input, Mapping):
        server = raw_input.get("server")
        tool = raw_input.get("tool")
        if type(server) is str and type(tool) is str:
            if server == BREAD_MCP_SERVER_NAME and tool in allowed_tools:
                return tool
            return None
    kind = _enum_value(_field(tool_call, "kind")).replace("-", "_").lower()
    if kind in _NON_MCP_TOOL_KINDS:
        return None
    for label in (
        _field(tool_call, "name", "tool_name", "toolName"),
        _field(tool_call, "title"),
    ):
        tool = _tool_from_label(label, allowed_tools)
        if tool is not None:
            return tool
    return None


def _scoped_replaylab_permission_option(
    state: _ACPClientState,
    tool_call: Any,
    options: list[Any],
) -> Any | None:
    """Select only a Host-exposed ReplayLab MCP permission option.

    The request must name the first-party MCP server and one tool from this
    session's Host allow-list (see ``_replaylab_tool_for_permission`` for the
    accepted shapes); option names are presentation and are ignored.
    """

    if _replaylab_tool_for_permission(tool_call, state.allowed_tools) is None:
        return None
    for wanted_kind in ("allow_once", "allow_always"):
        for option in options:
            kind = _enum_value(_field(option, "kind")).replace("-", "_").lower()
            if kind == wanted_kind:
                return option
    return None


def _validate_scoped_permission_request(
    state: _ACPClientState,
    session_id: str,
    tool_call: Any,
    options: list[Any],
) -> str:
    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > 256
        or "\0" in session_id
        or session_id != state.session_id
    ):
        return "session mismatch"
    raw_tool_call_id = _field(tool_call, "tool_call_id", "toolCallId")
    if type(raw_tool_call_id) is not str:
        return "invalid tool-call id"
    tool_call_id = raw_tool_call_id.strip()
    if not tool_call_id or len(tool_call_id) > 256 or "\0" in tool_call_id:
        return "invalid tool-call id"
    if tool_call_id in state.permission_ids:
        return "duplicate tool-call id"
    if not isinstance(options, list) or not 1 <= len(options) <= 32:
        return "invalid option count"
    option_ids: set[str] = set()
    for option in options:
        raw_option_id = _field(option, "option_id", "optionId")
        raw_name = _field(option, "name")
        if type(raw_option_id) is not str or type(raw_name) is not str:
            return "invalid permission option"
        option_id = raw_option_id.strip()
        name = raw_name.strip()
        kind = _enum_value(_field(option, "kind")).replace("-", "_").lower()
        if (
            not option_id
            or len(option_id) > 256
            or "\0" in option_id
            or option_id in option_ids
            or not name
            or len(name) > 512
            or "\0" in name
            or kind
            not in {"allow_once", "allow_always", "reject_once", "reject_always"}
        ):
            return "invalid permission option"
        option_ids.add(option_id)
    state.permission_ids.add(tool_call_id)
    return ""


def _update_kind(update: Any) -> str:
    """Return only the stable ACP session-update discriminator."""

    value = _field(update, "session_update", "sessionUpdate")
    if isinstance(value, str):
        return value.strip().casefold()
    # Keep a small compatibility fallback for SDK objects from ACP releases
    # which did not expose the discriminator as a normal attribute.
    return {
        "AgentMessageChunk": "agent_message_chunk",
        "AgentThoughtChunk": "agent_thought_chunk",
        "AvailableCommandsUpdate": "available_commands_update",
        "AgentPlanUpdate": "plan",
        "AgentPlanContentUpdate": "plan_update",
        "AgentPlanRemovedUpdate": "plan_removed",
        "ToolCallStart": "tool_call",
        "ToolCallProgress": "tool_call_update",
        "ToolCallUpdate": "tool_call_update",
    }.get(type(update).__name__, "")


def _opaque_activity_id(
    values: dict[str, str], raw_value: Any, prefix: str
) -> tuple[str, str]:
    """Map a provider id to a bounded opaque id that cannot leak its contents."""

    if isinstance(raw_value, str) and raw_value and len(raw_value) <= 256:
        key = raw_value
    else:
        key = f"__anonymous_{len(values) + 1}"
    activity_id = values.get(key)
    if activity_id is not None:
        return key, activity_id
    if len(values) >= _MAX_ACP_TIMELINE_ACTIVITIES:
        return "", ""
    activity_id = f"acp-{prefix}-{len(values) + 1}"
    values[key] = activity_id
    return key, activity_id


def _agent_reported_event(
    *,
    activity_id: str,
    activity_kind: str,
    title: str,
    status: str,
    lifecycle: str,
    detail: str = "",
) -> dict[str, Any]:
    """Build a public ACP row whose provenance cannot be mistaken for Host audit."""

    return {
        "activityId": activity_id,
        "activityKind": activity_kind,
        "activityTitle": title[:256],
        "activityDetail": detail[:4096],
        "publicDetail": detail[:4096],
        "status": status if status in {"running", "completed", "failed"} else "running",
        "lifecycle": (
            lifecycle
            if lifecycle in {"started", "updated", "completed"}
            else "updated"
        ),
        "elapsedSeconds": 0,
        # ACP notifications are provider claims.  Only ReplayLab's separate
        # MCP audit can establish that a tool actually ran and was validated.
        "verification": "agentReported",
        "statusSource": "agent",
        "hostVerified": False,
    }


def _agent_status(value: Any, fallback: str = "running") -> str:
    status = value if isinstance(value, str) else _enum_value(value)
    normalized = status.strip().replace("-", "_").casefold()
    if normalized in {"completed", "success", "succeeded"}:
        return "completed"
    if normalized in {"failed", "error", "cancelled", "canceled", "declined"}:
        return "failed"
    if normalized in {"pending", "in_progress", "running", "started"}:
        return "running"
    return fallback if fallback in {"running", "completed", "failed"} else "running"


def _plan_entries(update: Any, update_kind: str) -> Sequence[Any]:
    if update_kind == "plan":
        entries = _field(update, "entries")
    elif update_kind == "plan_update":
        entries = _field(_field(update, "plan"), "entries")
    else:
        entries = None
    if isinstance(entries, Sequence) and not isinstance(
        entries, (str, bytes, bytearray)
    ):
        return entries[:32]
    return ()


def _plan_key(update: Any, update_kind: str) -> Any:
    if update_kind == "plan":
        return "__legacy_plan"
    if update_kind == "plan_removed":
        return _field(update, "plan_id", "planId")
    plan = _field(update, "plan")
    return _field(plan, "plan_id", "planId")


def _plan_summary(entries: Sequence[Any]) -> tuple[str, str, dict[str, int]]:
    counts = {"pending": 0, "in_progress": 0, "completed": 0}
    for entry in entries:
        value = _field(entry, "status")
        status = value if isinstance(value, str) else _enum_value(value)
        status = status.strip().replace("-", "_").casefold()
        if status in counts:
            counts[status] += 1
    total = sum(counts.values())
    if not total:
        return "running", "", {"total": 0, **counts}
    overall = "completed" if counts["completed"] == total else "running"
    detail = (
        f"Steps: {total}; pending: {counts['pending']}; "
        f"in progress: {counts['in_progress']}; completed: {counts['completed']}"
    )
    return overall, detail, {"total": total, **counts}


def _safe_public_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or len(name) > _MAX_ACP_ARGUMENT_NAME_CHARS or not name.isascii():
        return ""
    if not (name[0].isalnum() or name[0] == "_"):
        return ""
    if any(not (char.isalnum() or char in "_.-") for char in name):
        return ""
    compact = "".join(char for char in name.casefold() if char.isalnum())
    forbidden = (
        "apikey",
        "accesskey",
        "privatekey",
        "key",
        "token",
        "secret",
        "password",
        "passwd",
        "credential",
        "authorization",
        "auth",
        "cookie",
        "rawinput",
        "rawoutput",
        "content",
        "location",
        "path",
        "uri",
        "command",
        "cwd",
        "environment",
        "error",
        "output",
        "result",
        "python",
        "repr",
        "class",
        "object",
    )
    if any(fragment in compact for fragment in forbidden):
        return ""
    return name


def _argument_names(update: Any) -> tuple[str, ...]:
    """Inspect only input object keys; provider-controlled values stay untouched."""

    raw_input = _field(update, "raw_input", "rawInput")
    if not isinstance(raw_input, Mapping):
        return ()
    nested = _field(raw_input, "arguments", "args", "input")
    values = nested if isinstance(nested, Mapping) else raw_input
    names: list[str] = []
    for raw_name in values:
        if len(names) >= _MAX_ACP_ARGUMENT_NAMES:
            break
        name = _safe_public_name(raw_name)
        if name and name not in names:
            names.append(name)
    return tuple(sorted(names, key=str.casefold))


def _safe_tool_name_from_title(update: Any, allowed_tools: frozenset[str]) -> str:
    """Recognize only exact, configured Bread MCP tool spellings (title or name).

    Accepts ``Bread:<tool>`` / ``Mcp(Bread:<tool>)`` plus the vendor spellings
    ``_tool_from_label`` knows (``mcp__Bread__<tool>`` from Claude Code,
    ``<tool> (Bread MCP Server)`` from Gemini CLI), so the Agent Monitor shows
    the real tool name instead of "tool activity".
    """

    safe = frozenset(
        configured for configured in allowed_tools
        if _safe_public_name(configured) == configured
    )
    title = _field(update, "title")
    if isinstance(title, str) and len(title) <= 256:
        for name in sorted(safe):
            if title in {
                name,
                f"Bread.{name}",
                f"Bread:{name}",
                f"Mcp(Bread:{name})",
                f"MCP(Bread:{name})",
            }:
                return name
    for label in (_field(update, "name", "tool_name", "toolName"), title):
        name = _tool_from_label(label, safe)
        if name:
            return name
    return ""


def _safe_tool_kind(value: Any, fallback: str = "other") -> str:
    kind = value if isinstance(value, str) else _enum_value(value)
    kind = kind.strip().replace("-", "_").casefold()
    if kind in {
        "read",
        "edit",
        "delete",
        "move",
        "search",
        "execute",
        "think",
        "fetch",
        "switch_mode",
        "other",
    }:
        return kind
    return fallback


def _safe_native_tool_name(update: Any, provider_id: str) -> str:
    """Cursor's technical tool identifier is a provider claim, not a grant."""
    raw = _field(update, "raw_input", "rawInput")
    if provider_id != "cursor-acp" or not isinstance(raw, Mapping):
        return ""
    return _safe_public_name(raw.get("_toolName"))


def _native_tool_kind(name: str, kind: str) -> str:
    if kind != "other":
        return kind
    return {
        "read": "read", "readfile": "read", "read_file": "read",
        "write": "edit", "writefile": "edit", "write_file": "edit",
        "edit": "edit", "editfile": "edit", "strreplace": "edit",
        "shell": "execute", "exec": "execute", "bash": "execute", "execute": "execute",
    }.get(name.casefold(), kind)


def _generic_tool_title(kind: str, status: str) -> str:
    label = {
        "read": "read activity",
        "edit": "edit activity",
        "delete": "delete activity",
        "move": "move activity",
        "search": "search activity",
        "execute": "execution activity",
        "think": "thinking activity",
        "fetch": "fetch activity",
        "switch_mode": "mode switch",
        "other": "tool activity",
    }.get(kind, "tool activity")
    if status == "completed":
        return f"Agent reported {label} completed"
    if status == "failed":
        return f"Agent reported {label} failed"
    return f"Agent reported {label} running"


async def _handle_public_timeline_update(
    state: _ACPClientState, update: Any, update_kind: str
) -> None:
    callback = state.on_timeline
    if callback is None or state.callback_error is not None:
        return
    if state.timeline_events >= _MAX_ACP_TIMELINE_EVENTS:
        return

    event: dict[str, Any] | None = None
    if update_kind in {"plan", "plan_update", "plan_removed"}:
        key, activity_id = _opaque_activity_id(
            state.plan_activity_ids,
            _plan_key(update, update_kind),
            "plan",
        )
        if not activity_id:
            return
        previous = state.plan_statuses.get(key, "")
        plan_counts: dict[str, int] | None = None
        if update_kind == "plan_removed":
            status = "completed"
            detail = ""
            title = "Agent reported plan removed"
        else:
            status, detail, plan_counts = _plan_summary(
                _plan_entries(update, update_kind)
            )
            if status == "completed":
                title = "Agent reported plan completed"
            elif previous:
                title = "Agent reported a plan update"
            else:
                title = "Agent reported a plan"
        lifecycle = (
            "completed"
            if status in {"completed", "failed"} or update_kind == "plan_removed"
            else ("updated" if previous else "started")
        )
        state.plan_statuses[key] = status
        event = _agent_reported_event(
            activity_id=activity_id,
            activity_kind="plan",
            title=title,
            detail=detail,
            status=status,
            lifecycle=lifecycle,
        )
        if plan_counts is not None:
            event["planStepCounts"] = {
                "total": plan_counts["total"],
                "pending": plan_counts["pending"],
                "inProgress": plan_counts["in_progress"],
                "completed": plan_counts["completed"],
            }

    elif update_kind in {"tool_call", "tool_call_update"}:
        key, activity_id = _opaque_activity_id(
            state.tool_activity_ids,
            _field(update, "tool_call_id", "toolCallId"),
            "tool",
        )
        if not activity_id:
            return
        previous_status = state.tool_statuses.get(key, "running")
        status = _agent_status(_field(update, "status"), previous_status)
        kind = _safe_tool_kind(
            _field(update, "kind"), state.tool_kinds.get(key, "other")
        )
        target_name = _safe_tool_name_from_title(update, state.allowed_tools)
        if not target_name:
            target_name = state.tool_targets.get(key, "")
        if target_name and status == "completed":
            from ..tool_output import mcp_result_failed
            if mcp_result_failed(_field(update, "raw_output", "rawOutput")):
                status = "failed"
        native_name = (_safe_native_tool_name(update, state.provider_id)
                       or state.native_tool_targets.get(key, "")) if not target_name else ""
        kind = _native_tool_kind(native_name, kind)
        names = _argument_names(update)
        if not names:
            names = state.tool_argument_names.get(key, ())
        state.tool_statuses[key] = status
        state.tool_kinds[key] = kind
        if target_name:
            state.tool_targets[key] = target_name
        if native_name:
            state.native_tool_targets[key] = native_name
        if names:
            state.tool_argument_names[key] = names
        # Keep a digest only. Provider-native commands/files may not produce a
        # Bread receipt, but distinct real work must still advance self-review.
        raw_input = _field(update, "raw_input", "rawInput")
        if not target_name and raw_input is not None:
            from ..task_review import work_identity
            state.tool_input_identities[key] = work_identity(raw_input)

        lifecycle = (
            "completed"
            if status in {"completed", "failed"}
            else ("started" if update_kind == "tool_call" else "updated")
        )
        detail = "Arguments: " + ", ".join(names) if names else ""
        if target_name:
            target = f"ReplayLab.{target_name}"
            if status == "completed":
                title = f"Agent reported {target} completed"
            elif status == "failed":
                title = f"Agent reported {target} failed"
            else:
                title = f"Agent reported calling {target}"
            event = _agent_reported_event(
                activity_id=activity_id,
                activity_kind="mcpToolCall",
                title=title,
                detail=detail,
                status=status,
                lifecycle=lifecycle,
            )
            event["activityTarget"] = target
            if (target_name in {"validate_frame_render_flow", "publish_frame_render_flow"}
                    and _enum_value(_field(update, "status")).casefold() == "pending"):
                event["activityTitleId"] = "task.frame-flow.waiting-for-arguments"
                event["activityTitle"] = "Waiting for the model to submit frame diagram arguments"
        else:
            event = _agent_reported_event(
                activity_id=activity_id,
                activity_kind="toolCall",
                title=_generic_tool_title(kind, status),
                detail=detail,
                status=status,
                lifecycle=lifecycle,
            )
            if native_name:
                event["activityTarget"] = native_name
        event["toolKind"] = kind
        event["argumentNames"] = list(names)
        if not target_name and lifecycle == "completed":
            from ..task_review import work_identity
            raw_output = _field(update, "raw_output", "rawOutput")
            content = _field(update, "content")
            event["workIdentity"] = work_identity({"input": state.tool_input_identities.get(key),
                                                   "output": raw_output, "content": content, "status": status})

    if event is None:
        return
    state.timeline_events += 1
    event["sequence"] = state.timeline_events
    try:
        result = callback(dict(event))
        if inspect.isawaitable(result):
            await result
    except Exception:
        # Do not reflect a callback's exception text into the task failure UI;
        # it may contain a Python repr, filesystem path, or credential value.
        state.callback_error = ConnectorError("ACP public timeline callback failed")
        state.abort_event.set()


def _is_agent_message(update: Any) -> bool:
    return _update_kind(update) == "agent_message_chunk"


def _extract_text(update: Any) -> str:
    content = update.get("content") if isinstance(update, dict) else getattr(update, "content", None)
    if isinstance(content, dict):
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            return content["text"]
        return ""
    if getattr(content, "type", None) == "text":
        value = getattr(content, "text", "")
        return value if isinstance(value, str) else ""
    return ""


def _extract_provider_error_text(update: Any) -> str:
    """Read a bounded vendor error notification without exposing its payload."""

    text = _extract_text(update)
    if text:
        return text[:16384]
    for name in ("message", "error", "detail", "reason"):
        value = _field(update, name)
        if isinstance(value, str) and value.strip():
            return value[:16384]
        if isinstance(value, Mapping):
            nested = _field(value, "message", "detail", "reason")
            if isinstance(nested, str) and nested.strip():
                return nested[:16384]
    return "provider error notification"


def _stop_reason(response: Any) -> str:
    if isinstance(response, dict):
        value = response.get("stopReason", response.get("stop_reason", ""))
    else:
        value = getattr(response, "stop_reason", "")
    return str(value or "unknown")


def _connection_models(session: Any) -> list[str]:
    """Extract bounded model ids from stable and legacy ACP session shapes."""

    result: list[str] = []
    options = _field(session, "config_options", "configOptions") or []
    for option in options:
        category = _enum_value(_field(option, "category")).lower()
        option_id = str(_field(option, "id") or "")
        if category != "model" and option_id.lower() not in {"model", "models"}:
            continue
        _append_model_choices(
            result, _field(option, "options", "values") or [], depth=0
        )
        if len(result) >= 200:
            return result

    # Older ACP agents used a dedicated ``models.availableModels`` object.
    # Keep this read-only fallback so a current ReplayLab client can still
    # populate the selector without pretending every Agent supports the newer
    # stable session config-option API.
    legacy = _field(session, "models")
    _append_model_choices(
        result,
        _field(legacy, "available_models", "availableModels") or [],
        depth=0,
        value_names=("model_id", "modelId", "value", "id"),
    )
    return result


def _field(value: Any, *names: str) -> Any:
    if value is None:
        return None
    for name in names:
        if isinstance(value, dict):
            if name in value:
                return value[name]
        elif hasattr(value, name):
            return getattr(value, name)
    return None


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _append_model_choices(
    result: list[str],
    choices: Any,
    *,
    depth: int,
    value_names: tuple[str, ...] = ("value", "id"),
) -> None:
    if depth > 2 or not isinstance(choices, (list, tuple)):
        return
    for choice in choices:
        nested = _field(choice, "options")
        if nested is not None:
            _append_model_choices(
                result,
                nested,
                depth=depth + 1,
                value_names=value_names,
            )
        else:
            value = choice if isinstance(choice, str) else _field(choice, *value_names)
            model = str(value or "").strip()
            if model and len(model) <= 512 and model not in result:
                result.append(model)
        if len(result) >= 200:
            return


def _wire_agent_info(initialized: Any) -> dict[str, str] | None:
    info = getattr(initialized, "agent_info", None)
    if info is None and isinstance(initialized, dict):
        info = initialized.get("agentInfo", initialized.get("agent_info"))
    if info is None:
        return None
    if isinstance(info, dict):
        name = info.get("name", "")
        version = info.get("version", "")
    else:
        name = getattr(info, "name", "")
        version = getattr(info, "version", "")
    return {"name": str(name or "")[:256], "version": str(version or "")[:128]}


async def _wait_for(awaitable: Any, timeout: float, stage: str) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ConnectorError(f"{stage} timed out") from exc
    except Exception as exc:
        # ACP reserves this error for authentication. Custom JSON-RPC servers
        # also use -32000 generically, so require the protocol diagnostic too.
        if (getattr(exc, "code", None) == -32000
                and str(exc).strip().casefold() in {"authentication required", "auth required"}):
            raise AuthenticationError(f"{stage}: the ACP Agent requires sign-in") from exc
        raise
