"""
File-based IPC client for communicating with RenderDocUltra's built-in bridge.

Protocol (v2):
  1. Atomically publish requests/<request-id>.json
  2. Extension atomically claims and executes that request
  3. Extension writes responses/<same-name>.json
  4. We verify both the file name and response id, then delete our response

ReplayLab v2 bridge instances live under
``%LOCALAPPDATA%/ReplayLab/ipc-v2/instances/<instance-id>``. The client binds
each call to the most recently interacted-with live qrenderdoc instance, or to
``REPLAYLAB_INSTANCE_ID`` when the process owner explicitly selects one. The
legacy global mailbox remains a fallback during migration.
"""

import contextvars
import json
import os
import time
import tempfile
import threading
import uuid
import logging
import math
from contextlib import contextmanager
from typing import Any, Optional

logger = logging.getLogger("renderdoc-mcp")

LEGACY_IPC_DIR = os.path.join(tempfile.gettempdir(), "renderdoc_mcp_ipc")
_local_root = (os.environ.get("LOCALAPPDATA", "").strip()
               or os.environ.get("XDG_DATA_HOME", "").strip()
               or os.path.join(os.path.expanduser("~"), ".local", "share"))
IPC_ROOT = os.path.join(_local_root, "ReplayLab", "ipc-v2")
INSTANCE_ROOT = os.path.join(IPC_ROOT, "instances")
# Public legacy constants are retained for compatibility and for isolated
# protocol tests that patch one mailbox directly.
IPC_DIR = LEGACY_IPC_DIR
REQUEST_FILE = os.path.join(IPC_DIR, "request.json")
RESPONSE_FILE = os.path.join(IPC_DIR, "response.json")
READY_FILE = os.path.join(IPC_DIR, "ready")
REQUEST_DIR = os.path.join(IPC_DIR, "requests")
RESPONSE_DIR = os.path.join(IPC_DIR, "responses")
_LEGACY_REQUEST_DIR = REQUEST_DIR
_LEGACY_RESPONSE_DIR = RESPONSE_DIR
_CALL_BINDING: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "replaylab_direct_tool_call_binding", default=None
)
_FOREGROUND_YIELD_CHECKPOINT = contextvars.ContextVar(
    "bread_foreground_yield_checkpoint", default=None
)
_FOREGROUND_YIELD_MESSAGES = frozenset({
    "Bread background tool yielded after a user replay operation; retry it if still needed",
    "Bread background tool yielded to a user replay operation; retry it if still needed",
})


@contextmanager
def resume_after_foreground_yield(checkpoint):
    """Resume a pinned restore request after an explicit non-committing yield.

    This deliberately does not retry timeouts, cancellations, lifecycle changes,
    or arbitrary bridge failures. The bridge finishes/rolls back atomic replay
    transactions before yielding, and each retry is a new request with the latest
    visible EID while retaining the same instance and capture-session binding.
    """
    if not callable(checkpoint):
        raise TypeError("foreground yield recovery requires a control checkpoint")
    token = _FOREGROUND_YIELD_CHECKPOINT.set(checkpoint)
    try:
        yield
    finally:
        _FOREGROUND_YIELD_CHECKPOINT.reset(token)


@contextmanager
def bind_direct_tool_call(instance_id: str, capture_session_id: str):
    """Pin all nested IPC calls made by one in-process AgentHost tool.

    The external MCP process continues to use its ordinary constructor/env
    binding.  This context is only set by AgentHost's direct high-level
    workflow dispatcher and is local to that worker thread/context.
    """

    instance = str(instance_id or "").strip()
    session = str(capture_session_id or "").strip().lower()
    if not instance or instance != instance_id or "\0" in instance or len(instance) > 512:
        raise ValueError("direct Bread tool requires a pinned qrenderdoc instance")
    if session and (
        len(session) != 32
        or any(character not in "0123456789abcdef" for character in session)
    ):
        raise ValueError("direct Bread tool capture session is invalid")
    token = _CALL_BINDING.set((instance, session))
    try:
        yield
    finally:
        _CALL_BINDING.reset(token)


class IPCTransportError(RuntimeError):
    """A request could not obtain a readable reply from its transport."""


class IPCBridgeBusyError(IPCTransportError):
    """The call could not start or its pinned owner did not answer in time."""


class IPCBridgeTimeoutError(IPCTransportError):
    """The selected mailbox did not answer before the request deadline."""


class IPCBridgeUnavailableError(IPCTransportError):
    """The frozen v2 owner cannot be identified without changing transport."""


class IPCBridgeResponseError(RuntimeError):
    """A readable bridge rejection, distinct from a missing transport reply."""

    def __init__(self, code: Any, message: str):
        self.code = code
        self.message = str(message)
        super().__init__("Bridge error [%s]: %s" % (code, self.message))


class IPCClient:
    """Communicates with RenderDocUltra's built-in bridge via file IPC."""

    supports_shader_restore_batches = True

    @property
    def shader_restore_batch_limit(self) -> int:
        binding = _CALL_BINDING.get()
        instance = binding[0] if binding else self.instance_id
        if not instance:
            return 16
        manifest = self._manifest(os.path.join(INSTANCE_ROOT, instance), allow_stale_heartbeat=True)
        limit = (manifest or {}).get("shaderRestoreBatchLimit", 16)
        return 64 if type(limit) is int and 64 <= limit <= 256 else 16

    def __init__(self, timeout: float = 30.0, instance_id: str | None = None):
        self.timeout = timeout
        self.instance_id = (
            str(instance_id or os.environ.get("REPLAYLAB_INSTANCE_ID", "")).strip()
        )
        self._explicit_instance = bool(self.instance_id)
        # Every nested call (including direct ipc.call paths) inherits the
        # task's admitted capture, not only server._call convenience wrappers.
        # This changes only after an explicit lifecycle operation's receipt.
        raw_scope = os.environ.get("REPLAYLAB_TOOL_SCOPE", "")
        try:
            scope = json.loads(raw_scope) if raw_scope else {}
        except (TypeError, ValueError) as error:
            raise ValueError("Bread task capture scope is invalid") from error
        if not isinstance(scope, dict):
            raise ValueError("Bread task capture scope must be an object")
        self.capture_session_id = self._checked_session(scope.get("capture_session_id", ""))
        # Keep calls ordered inside one MCP process. The v2 protocol also uses
        # per-request files, so independent processes cannot delete or consume
        # each other's response.
        self._mailbox_lock = threading.Lock()

    @property
    def supports_verified_restore_mapping(self) -> bool:
        binding = _CALL_BINDING.get()
        instance = binding[0] if binding else self.instance_id
        if not instance:
            return False
        manifest = self._manifest(os.path.join(INSTANCE_ROOT, instance), allow_stale_heartbeat=True)
        return (manifest or {}).get("verifiedRestoreMappingCheck") is True

    @staticmethod
    def _checked_session(value: Any) -> str:
        if not isinstance(value, str) or (value and (
                len(value) != 32 or any(c not in "0123456789abcdef" for c in value))):
            raise ValueError("Bread task capture session is invalid")
        return value

    def _accept_capture_transition(self, method: str, params: dict, result: Any,
                                   expected: str, direct_binding: bool) -> None:
        """Admit only successful Host-owned open/recovery receipts.

        A context read, incidental UI switch, error or late foreign response
        cannot move the task. Direct in-process jobs retain their own binding.
        """
        if direct_binding or not isinstance(result, dict) or result.get("error"):
            return
        if method == "recover_replay":
            admitted = (result.get("schema") == "bread-replay-recovery/v1"
                        and result.get("recovered") is True
                        and result.get("status") == "recovered"
                        and bool(expected)
                        and result.get("previousCaptureSessionId") == expected)
        elif method == "open_capture":
            requested, opened = params.get("capture_path"), result.get("opened")
            admitted = (isinstance(requested, str) and isinstance(opened, str)
                        and bool(requested) and bool(opened)
                        and os.path.normcase(os.path.abspath(requested))
                        == os.path.normcase(os.path.abspath(opened)))
        else:
            return
        if admitted:
            session = self._checked_session(result.get("captureSessionId"))
            if not session:
                raise ValueError("Bread capture transition omitted its new session")
            self.capture_session_id = session

    @contextmanager
    def bind_capture_session(self, capture_session_id: str):
        """Keep a workflow on its admitted v2 owner, including stale heartbeats."""

        existing = _CALL_BINDING.get()
        session = str(capture_session_id or "").strip().lower()
        if existing is not None:
            if existing[1] and session and existing[1] != session:
                raise RuntimeError("Bread workflow attempted to change its frozen capture session")
            with bind_direct_tool_call(existing[0], existing[1] or session):
                yield
            return
        if session and not self._explicit_instance:
            # HTTP restore already owns a frozen capture session. Its matching
            # live process can be busy before admission, so fresh-heartbeat
            # discovery or the most recent other window must not redirect it.
            try:
                names = os.listdir(INSTANCE_ROOT)
            except FileNotFoundError:
                names = []
            except OSError as exc:
                raise IPCBridgeUnavailableError(
                    "Bread frozen capture owner discovery is unreadable: %s" % exc) from exc
            owners = []
            v2_instances_present = False
            for name in names:
                directory = os.path.join(INSTANCE_ROOT, name)
                if not os.path.isdir(directory):
                    continue
                v2_instances_present = True
                manifest = self._manifest(
                    directory, allow_stale_heartbeat=True)
                if (manifest is not None and manifest.get("instanceId") == name
                        and manifest.get("captureLoaded") is True
                        and manifest.get("captureSessionId") == session):
                    owners.append(name)
            if len(owners) > 1:
                raise RuntimeError("Multiple Bread qrenderdoc instances claim the frozen capture session")
            if owners:
                self.instance_id = owners[0]
                with bind_direct_tool_call(owners[0], session):
                    yield
                return
            if v2_instances_present or self.instance_id:
                # A missing/unreadable target manifest is no evidence of a
                # capture change. Other windows and the legacy mailbox cannot
                # acquire this frozen request. Residual v2 metadata also fails
                # closed; only a genuinely v2-free environment is legacy-only.
                raise IPCBridgeUnavailableError(
                    "Bread frozen capture owner is unavailable; the request was not sent. "
                    "Keep the current capture binding instead of selecting another window or legacy mailbox.")
        selected = self._instance_mailbox()
        if selected is None:
            # A legacy-only embedder has no v2 owner to pin. It retains its
            # mailbox compatibility, but never qualifies for an owner-bound retry.
            yield
            return
        selected_instance = os.path.basename(os.path.dirname(selected[0]))
        with bind_direct_tool_call(selected_instance, session):
            yield

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        if os.name == "nt":
            # ``os.kill(pid, 0)`` can surface WinError 87 as ``SystemError`` in
            # a PyInstaller-frozen process. The packaged stdio MCP uses this
            # path to select a live qrenderdoc mailbox, so query a read-only
            # process handle just like the ReplayLab owner monitor does.
            try:
                import ctypes

                process_query_limited_information = 0x1000
                synchronize = 0x00100000
                still_active = 259
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.OpenProcess.argtypes = [
                    ctypes.c_uint32,
                    ctypes.c_int,
                    ctypes.c_uint32,
                ]
                kernel32.OpenProcess.restype = ctypes.c_void_p
                kernel32.GetExitCodeProcess.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_uint32),
                ]
                kernel32.GetExitCodeProcess.restype = ctypes.c_int
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel32.CloseHandle.restype = ctypes.c_int
                handle = kernel32.OpenProcess(
                    process_query_limited_information | synchronize,
                    False,
                    int(pid),
                )
                if not handle:
                    return ctypes.get_last_error() == 5
                try:
                    exit_code = ctypes.c_uint32(0)
                    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return False
                    return int(exit_code.value) == still_active
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except (OSError, ProcessLookupError):
            return False

    @staticmethod
    def _manifest(directory: str, *, allow_stale_heartbeat: bool = False) -> dict | None:
        path = os.path.join(directory, "instance.json")
        try:
            with open(path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(value, dict):
            return None
        try:
            heartbeat = int(value.get("heartbeatUnixMs", 0))
            pid = int(value.get("pid", 0))
        except (TypeError, ValueError):
            return None
        if value.get("stopped") or heartbeat <= 0 or pid <= 0:
            return None
        # Discovery avoids blocked integrations. An explicitly pinned owner can
        # remain alive while replay blocks its UI-thread heartbeat; let that
        # owner's ordinary bounded request wait distinguish busy from dead.
        if not allow_stale_heartbeat and int(time.time() * 1000) - heartbeat > 5000:
            return None
        if not IPCClient._process_alive(pid):
            return None
        return value

    def bound_instance_state(self, instance_id: str | None = None) -> dict | None:
        """Read the pinned owner's process state without pinging or rebinding."""

        binding = _CALL_BINDING.get()
        instance_id = instance_id or (binding[0] if binding is not None else (
            self.instance_id if self._explicit_instance else ""
        ))
        if not instance_id:
            return None
        manifest = self._manifest(
            os.path.join(INSTANCE_ROOT, instance_id), allow_stale_heartbeat=True
        )
        if manifest is None or manifest.get("instanceId") != instance_id:
            return {"instanceId": instance_id, "alive": False, "heartbeatFresh": False}
        return {
            "instanceId": instance_id,
            "pid": int(manifest["pid"]),
            "alive": True,
            "heartbeatFresh": int(time.time() * 1000) - int(manifest["heartbeatUnixMs"]) <= 5000,
        }

    def _instance_mailbox(self) -> tuple[str, str] | None:
        binding = _CALL_BINDING.get()
        if binding is not None:
            instance_id, _capture_session_id = binding
            directory = os.path.join(INSTANCE_ROOT, instance_id)
            manifest = self._manifest(directory, allow_stale_heartbeat=True)
            if manifest is None or manifest.get("instanceId") != instance_id:
                raise RuntimeError(
                    "Requested Bread qrenderdoc instance is not alive: %s"
                    % instance_id
                )
            return (
                os.path.join(directory, "requests"),
                os.path.join(directory, "responses"),
            )

        candidates: list[tuple[tuple[int, int, int], str, dict]] = []
        if self._explicit_instance:
            directory = os.path.join(INSTANCE_ROOT, self.instance_id)
            manifest = self._manifest(directory, allow_stale_heartbeat=True)
            if manifest is None or manifest.get("instanceId") != self.instance_id:
                raise RuntimeError(
                    "Requested Bread qrenderdoc instance is not alive: %s"
                    % self.instance_id
                )
            candidates.append(((0, 0, 0), directory, manifest))
        else:
            # An implicitly selected instance is only the result of the last
            # lookup, not a binding. Re-scan on every call so a newly opened
            # capture can take precedence over an older capture-less window.
            try:
                names = os.listdir(INSTANCE_ROOT)
            except OSError:
                names = []
            for name in names:
                directory = os.path.join(INSTANCE_ROOT, name)
                if not os.path.isdir(directory):
                    continue
                manifest = self._manifest(directory)
                if manifest is None:
                    continue
                try:
                    score = (
                        int(bool(manifest.get("captureLoaded"))),
                        int(manifest.get("lastActivityUnixMs", 0)),
                        int(manifest.get("startedUnixMs", 0)),
                    )
                except (TypeError, ValueError):
                    continue
                candidates.append((score, directory, manifest))
        if not candidates:
            if not self._explicit_instance:
                self.instance_id = ""
            return None
        _score, directory, manifest = max(candidates, key=lambda item: item[0])
        self.instance_id = str(manifest.get("instanceId", ""))
        return (
            os.path.join(directory, "requests"),
            os.path.join(directory, "responses"),
        )

    def _mailbox(self) -> tuple[str, str]:
        # Unit tests and compatibility embedders may explicitly replace the
        # public mailbox constants with an isolated directory.
        if REQUEST_DIR != _LEGACY_REQUEST_DIR or RESPONSE_DIR != _LEGACY_RESPONSE_DIR:
            return REQUEST_DIR, RESPONSE_DIR
        selected = self._instance_mailbox()
        if selected is not None:
            return selected
        return REQUEST_DIR, RESPONSE_DIR

    def is_bridge_alive(self) -> bool:
        try:
            resp = self.call("ping", {}, timeout=3.0)
            return resp is not None and "status" in resp
        except Exception:
            return False

    def call(self, method: str, params: dict, timeout: Optional[float] = None) -> Any:
        budget = float(self.timeout if timeout is None else timeout)
        if not math.isfinite(budget) or budget < 0:
            raise ValueError("IPC timeout must be a finite non-negative duration")
        deadline = time.monotonic() + budget
        values = dict(params or {})
        binding = _CALL_BINDING.get()
        session = binding[1] if binding is not None else self.capture_session_id
        if session:
            expected = values.get("__replaylab_expected_capture_session_id")
            if expected not in (None, "", session):
                raise RuntimeError(
                    "Bread direct tool attempted to change its frozen capture session"
                )
            values["__replaylab_expected_capture_session_id"] = session
        # A queued request must not gain a fresh timeout after every preceding
        # replay call. Preserve serialization, but charge queueing and response
        # waiting to this single deadline.
        if not self._mailbox_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise self._unsent_busy_error(method, budget)
        try:
            if time.monotonic() >= deadline:
                raise self._unsent_busy_error(method, budget)
            retry_count = 0
            while True:
                try:
                    result = self._call_locked(method, values, budget, deadline=deadline)
                    self._accept_capture_transition(method, values, result, session, binding is not None)
                    return result
                except IPCBridgeResponseError as exc:
                    checkpoint = _FOREGROUND_YIELD_CHECKPOINT.get()
                    if (checkpoint is None or binding is None or not binding[1]
                            or exc.code != -32010 or exc.message not in _FOREGROUND_YIELD_MESSAGES):
                        raise
                    checkpoint("after foreground replay yield")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    # Leave foreground interaction responsive without tight
                    # mailbox polling or granting a fresh operation timeout.
                    time.sleep(min(0.05 * (2 ** min(retry_count, 4)), 0.5, remaining))
                    retry_count += 1
                    checkpoint("before foreground replay resume")
                    if time.monotonic() >= deadline:
                        raise
        finally:
            self._mailbox_lock.release()

    @staticmethod
    def _unsent_busy_error(method: str, timeout: float) -> IPCBridgeBusyError:
        return IPCBridgeBusyError(
            "Bread qrenderdoc bridge is busy: %s could not start within %.1fs including "
            "the replay queue. The request was not sent. Keep the current "
            "instance binding; queueing does not require launching another instance."
            % (method, timeout)
        )

    def _call_locked(
        self, method: str, params: dict, timeout: Optional[float] = None,
        *, deadline: Optional[float] = None,
    ) -> Any:
        if timeout is None:
            timeout = self.timeout
        if deadline is None:
            deadline = time.monotonic() + timeout

        request_dir, response_dir = self._mailbox()
        # Record the owner of this request, even when ordinary discovery selected
        # it implicitly. Timeout reporting must not rediscover another mailbox.
        owner_directory = os.path.dirname(os.path.abspath(request_dir))
        selected_owner = os.path.basename(owner_directory) if (
            os.path.basename(request_dir) == "requests"
            and os.path.normcase(os.path.dirname(owner_directory))
            == os.path.normcase(os.path.abspath(INSTANCE_ROOT))
        ) else None
        os.makedirs(request_dir, exist_ok=True)
        os.makedirs(response_dir, exist_ok=True)

        # Build request
        request_id = str(uuid.uuid4())
        request = {"id": request_id, "method": method, "params": params}
        request_json = json.dumps(request, ensure_ascii=False, default=str)
        file_name = request_id + ".json"
        request_path = os.path.join(request_dir, file_name)
        response_path = os.path.join(response_dir, file_name)
        temporary_path = request_path + ".tmp.%d" % os.getpid()

        if time.monotonic() >= deadline:
            raise self._unsent_busy_error(method, timeout)

        # A request only becomes visible after the complete UTF-8 document has
        # been flushed and atomically renamed into the queue.
        with open(temporary_path, "w", encoding="utf-8") as f:
            f.write(request_json)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, request_path)

        # Wait for our matching response. Cleanup is deliberately scoped to
        # this UUID, including protocol errors and bridge-side failures.
        try:
            while time.monotonic() < deadline:
                if os.path.exists(response_path):
                    # V2 responses are atomically published. A legacy writer
                    # that is still writing is handled by bounded JSON retry.
                    try:
                        with open(response_path, "r", encoding="utf-8") as f:
                            response = json.load(f)

                        response_id = response.get("id")
                        if response_id != request_id:
                            raise RuntimeError(
                                "Bridge protocol error: response id %r does not match %r"
                                % (response_id, request_id)
                            )

                        if "error" in response:
                            err = response["error"]
                            raise IPCBridgeResponseError(
                                err.get("code", "?"), err.get("message", str(err)))
                        return response.get("result", response)
                    except (json.JSONDecodeError, PermissionError, FileNotFoundError):
                        # Antivirus/indexing or a legacy writer can briefly
                        # deny/read-rename this UUID's response on Windows.
                        # Retry only the read within the existing deadline;
                        # never resend a mutation after dispatch.
                        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                        continue
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

            owner = (self.bound_instance_state(selected_owner) if selected_owner
                     else self.bound_instance_state())
            if owner is not None and owner["alive"]:
                raise IPCBridgeBusyError(
                    "Bread qrenderdoc instance %s is still running but busy or "
                    "unresponsive: no response to %s within %.1fs. Keep this instance "
                    "binding; a missed response does not require launching another instance."
                    % (owner["instanceId"], method, timeout)
                )
            raise IPCBridgeTimeoutError(
                "Timeout (%.0fs). Make sure RenderDoc is running with capture loaded. "
                "Open Tools > Bread AI, then run Check Connection." % timeout)
        finally:
            # Never touch another process's request/response or the legacy
            # global mailbox.
            for owned_path in (request_path, response_path, temporary_path):
                try:
                    if os.path.exists(owned_path):
                        os.remove(owned_path)
                except OSError:
                    pass
