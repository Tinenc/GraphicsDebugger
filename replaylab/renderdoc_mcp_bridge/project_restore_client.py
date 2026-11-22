"""Bounded loopback client for qrenderdoc's private ReplayLab service.

This module runs in qrenderdoc's embedded Python 3.6.  That compact runtime
does not ship ``_socket``, so requests use WinHTTP directly instead of
``urllib``.  The client talks only to the singleton Host state published under
ReplayLab's local runtime directory, reconstructs a fixed loopback request,
disables redirects/ambient authentication before attaching the bearer secret,
and bounds every response and wall-clock wait.
"""

from __future__ import absolute_import, print_function

import ctypes
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid


RESTORE_JOB_SCHEMA = "replaylab-project-restore-job/v1"
RESTORE_JOB_PATH = "/replaylab/project-restore"
# Keep these values aligned with the private Host's bounded restore contract.
# The client allowance includes the Host's execution deadline, the longest
# already-running export IPC (cooperative cancellation can only stop after it
# returns or reaches its own timeout), rollback/cleanup grace, and a final
# minute for the authenticated HTTP response and bounded progress polling.
PROJECT_RESTORE_DEADLINE_SECONDS = 1800.0
PROJECT_RESTORE_MAX_INFLIGHT_SECONDS = 1800.0
PROJECT_RESTORE_CLEANUP_GRACE_SECONDS = 300.0
DEFAULT_PROJECT_RESTORE_CLIENT_TIMEOUT_SECONDS = 3960.0
PROJECT_OPERATION_SCHEMA = "replaylab-internal-project-operation/v1"
PROJECT_OPERATION_RESULT_SCHEMA = "replaylab-internal-project-operation-result/v1"
PROJECT_OPERATION_PATH = "/replaylab/internal/project-operation"
# Minimum Host package the client was written against. A newer patch or
# minor on the same major line stays compatible while protocol stays 1.
EXPECTED_HOST_VERSION = "1.13.121"
INTERNAL_HOST_STATE_PROTOCOL_VERSION = 1
INTERNAL_HOST_CAPABILITY_SCHEMA = "replaylab-internal-host-capabilities/v1"
EXPECTED_INTERNAL_CAPABILITY_VERSIONS = {
    "project-service": 1,
    "project-restore": 1,
    "shader-workspace-prepare": 1,
    "shader-source-save": 1,
    "shader-live-edit": 1,
    "asset-index": 1,
}
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_HOST_SEMVER = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")

# The internal Host advertises its route families here.  New families (for
# example "asset-index", 2026-09) are additive: a client requires the ones it
# uses and ignores the rest, so a newer Host never fails an older client.
REQUIRED_INTERNAL_CAPABILITIES = (
    "project-service", "project-restore", "shader-workspace-prepare",
    "shader-source-save", "shader-live-edit", "asset-index")


def _has_required_capabilities(value):
    if not isinstance(value, list) or len(value) > 32:
        return False
    if any(not isinstance(item, str) or len(item) > 64 for item in value):
        return False
    return all(item in value for item in REQUIRED_INTERNAL_CAPABILITIES)


def _has_required_capability_versions(schema, value):
    """Validate the wire contracts needed by this bundled bridge.

    A route name says only that something exists.  The schema map makes an
    incompatible fresh Host fail before a restore job is submitted, rather
    than discovering the mismatch once for every Shader in the capture.
    Unknown additive entries are accepted; required entries match exactly.
    """

    if schema != INTERNAL_HOST_CAPABILITY_SCHEMA or not isinstance(value, dict):
        return False
    if len(value) > 32:
        return False
    for name, version in value.items():
        if (not isinstance(name, str) or len(name) > 64 or
                isinstance(version, bool) or not isinstance(version, int) or
                version < 1 or version > 1000000):
            return False
    return all(value.get(name) == version
               for name, version in EXPECTED_INTERNAL_CAPABILITY_VERSIONS.items())


def _capability_contract_description(schema, value):
    """A bounded diagnostic for an untrusted Host capability declaration."""

    try:
        detail = json.dumps(
            {"schema": schema, "versions": value},
            ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        detail = "schema=%r versions=%r" % (schema, value)
    return detail if len(detail) <= 1024 else detail[:1024] + "...[truncated]"


def _parse_host_semver(value):
    if not isinstance(value, str):
        return None
    match = _HOST_SEMVER.fullmatch(value.strip())
    if match is None:
        return None
    parts = tuple(int(item) for item in match.groups())
    if any(part < 0 or part > 999999 for part in parts):
        return None
    return parts


def _host_runtime_version_compatible(received, expected):
    """True when a live Host is the same or a newer compatible package.

    Protocol remains the hard contract. A 1.13.9 Host must still load a
    client that expected 1.13.8; a 1.10 Host or a 2.x Host must not.
    """

    received_parts = _parse_host_semver(received)
    expected_parts = _parse_host_semver(expected)
    if received_parts is None or expected_parts is None:
        return False
    if received_parts[0] != expected_parts[0]:
        return False
    return received_parts >= expected_parts

MAX_STATE_BYTES = 64 * 1024
MAX_PROJECT_REQUEST_BYTES = 16 * 1024 * 1024
MAX_TOKEN_BYTES = 1024
MAX_PROGRESS_EVENTS = 256
MAX_PROGRESS_COUNT = 1000000000
MAX_RESULT_ITEMS = 65536
MAX_HOST_HEARTBEAT_AGE_MS = 5000
WINHTTP_CANCEL_SLICE_MS = 500
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
SESSION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ReplayLabRestoreClientError(RuntimeError):
    pass


class ReplayLabRestoreHostIncompatible(ReplayLabRestoreClientError):
    """A fresh/live Host belongs to a different ReplayLab runtime contract."""

    pass


class ReplayLabRestoreRequestUncertain(ReplayLabRestoreClientError):
    """The request deadline elapsed after the Host may have received it."""

    pass


class ReplayLabRestoreTransportError(ReplayLabRestoreClientError):
    """Preserve native transport facts without classifying diagnostic text."""

    def __init__(self, error, method, path):
        ReplayLabRestoreClientError.__init__(
            self, "Bread Host connection failed: %s" % str(error))
        self.winerror = getattr(error, "winerror", None)
        self.error_type = type(error).__name__
        self.method = method
        self.path = path


_RESTORE_LIFECYCLE = threading.Condition()
_RESTORE_GENERATION = 0
_RESTORE_STOPPING = False
_ACTIVE_CLIENTS = set()


def _publish_progress(callback, phase, current=0, total=0, message="", item=""):
    """Publish display-only progress without affecting restore correctness."""

    if callback is None:
        return
    try:
        callback({
            "phase": phase,
            "message": message,
            "current": current,
            "total": total,
            "item": item,
        })
    except Exception:
        # A UI observer must never cancel or weaken a physical restore.
        pass


def _winhttp_request_blocking(method, path, port, headers, body,
                              timeout_seconds, cancel_event,
                              expected_generation):
    """Perform one bounded request and close every handle on this same thread."""

    if os.name != "nt":
        raise ReplayLabRestoreClientError(
            "Bread project restore requires the Windows loopback Host")
    deadline = time.monotonic() + max(
        0.1, min(120.0, float(timeout_seconds)))
    winhttp = ctypes.WinDLL("winhttp", use_last_error=True)
    void_pointer = ctypes.c_void_p
    dword = ctypes.c_uint32

    winhttp.WinHttpOpen.argtypes = (
        ctypes.c_wchar_p, dword, ctypes.c_wchar_p, ctypes.c_wchar_p, dword)
    winhttp.WinHttpOpen.restype = void_pointer
    winhttp.WinHttpConnect.argtypes = (
        void_pointer, ctypes.c_wchar_p, ctypes.c_uint16, dword)
    winhttp.WinHttpConnect.restype = void_pointer
    winhttp.WinHttpOpenRequest.argtypes = (
        void_pointer, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, void_pointer, dword)
    winhttp.WinHttpOpenRequest.restype = void_pointer
    winhttp.WinHttpSetTimeouts.argtypes = (
        void_pointer, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
    winhttp.WinHttpSetTimeouts.restype = ctypes.c_int
    winhttp.WinHttpSetOption.argtypes = (
        void_pointer, dword, void_pointer, dword)
    winhttp.WinHttpSetOption.restype = ctypes.c_int
    winhttp.WinHttpSendRequest.argtypes = (
        void_pointer, ctypes.c_wchar_p, dword, void_pointer, dword, dword,
        ctypes.c_size_t)
    winhttp.WinHttpSendRequest.restype = ctypes.c_int
    winhttp.WinHttpReceiveResponse.argtypes = (void_pointer, void_pointer)
    winhttp.WinHttpReceiveResponse.restype = ctypes.c_int
    winhttp.WinHttpQueryHeaders.argtypes = (
        void_pointer, dword, ctypes.c_wchar_p, void_pointer,
        ctypes.POINTER(dword), ctypes.POINTER(dword))
    winhttp.WinHttpQueryHeaders.restype = ctypes.c_int
    winhttp.WinHttpQueryDataAvailable.argtypes = (
        void_pointer, ctypes.POINTER(dword))
    winhttp.WinHttpQueryDataAvailable.restype = ctypes.c_int
    winhttp.WinHttpReadData.argtypes = (
        void_pointer, void_pointer, dword, ctypes.POINTER(dword))
    winhttp.WinHttpReadData.restype = ctypes.c_int
    winhttp.WinHttpCloseHandle.argtypes = (void_pointer,)
    winhttp.WinHttpCloseHandle.restype = ctypes.c_int

    session = connection = request = None
    request_started = False

    def check_request_state():
        with _RESTORE_LIFECYCLE:
            stopping = _RESTORE_STOPPING
            generation_changed = (
                expected_generation is not None and
                _RESTORE_GENERATION != expected_generation)
        # ``None`` is reserved for one bounded best-effort DELETE after a
        # restore has already observed cancellation.  It must outlive the
        # lifecycle fence long enough to ask the Host to stop the owned job;
        # all ordinary authenticated requests remain fenced as before.
        cleanup_request = expected_generation is None
        if (cancel_event.is_set() or
                (not cleanup_request and (stopping or generation_changed))):
            raise ReplayLabRestoreClientError(
                "Bread project restore was cancelled during shutdown")
        if time.monotonic() >= deadline:
            if method == "POST":
                raise ReplayLabRestoreRequestUncertain(
                    "Bread Host request timed out")
            # Same fact as a native ERROR_WINHTTP_TIMEOUT (12002): this one
            # request took too long. Report it through the transport class so
            # a job poll can be retried instead of failing the whole restore.
            raise ReplayLabRestoreTransportError(
                ctypes.WinError(12002, "Host request timed out"), method, path)

    try:
        check_request_state()
        # WINHTTP_ACCESS_TYPE_NO_PROXY keeps this authenticated request on the
        # fixed 127.0.0.1 boundary even when the user has a system proxy.
        session = winhttp.WinHttpOpen(
            "RenderDocUltra-ReplayLab/1", 1, None, None, 0)
        if not session:
            raise ctypes.WinError(ctypes.get_last_error())
        # Synchronous WinHTTP handles may only be closed by their owning thread.
        # Short native waits plus the absolute deadline below keep shutdown
        # cooperative and bounded without a cross-thread WinHttpCloseHandle race.
        timeout_ms = max(100, min(
            WINHTTP_CANCEL_SLICE_MS,
            int(max(0.1, deadline - time.monotonic()) * 1000)))
        if not winhttp.WinHttpSetTimeouts(
                session, timeout_ms, timeout_ms, timeout_ms, timeout_ms):
            raise ctypes.WinError(ctypes.get_last_error())
        check_request_state()
        connection = winhttp.WinHttpConnect(
            session, "127.0.0.1", int(port), 0)
        if not connection:
            raise ctypes.WinError(ctypes.get_last_error())
        check_request_state()
        request = winhttp.WinHttpOpenRequest(
            connection, method, path, None, None, None, 0)
        if not request:
            raise ctypes.WinError(ctypes.get_last_error())
        check_request_state()

        # Never forward the bearer through a redirect or let a stale process on
        # a reused loopback port trigger ambient Negotiate/NTLM credentials.
        disabled_features = dword(
            0x2 | 0x4)  # WINHTTP_DISABLE_REDIRECTS | DISABLE_AUTHENTICATION
        if not winhttp.WinHttpSetOption(
                request, 63, ctypes.byref(disabled_features),
                ctypes.sizeof(disabled_features)):
            raise ctypes.WinError(ctypes.get_last_error())
        auto_logon_policy = dword(2)  # WINHTTP_AUTOLOGON_SECURITY_LEVEL_HIGH
        if not winhttp.WinHttpSetOption(
                request, 77, ctypes.byref(auto_logon_policy),
                ctypes.sizeof(auto_logon_policy)):
            raise ctypes.WinError(ctypes.get_last_error())
        check_request_state()

        header_text = headers or None
        body_buffer = ctypes.create_string_buffer(body) if body else None
        body_pointer = (ctypes.cast(body_buffer, void_pointer)
                        if body_buffer is not None else None)
        request_started = True
        if not winhttp.WinHttpSendRequest(
                request, header_text, len(headers or ""), body_pointer,
                len(body), len(body), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        check_request_state()
        if not winhttp.WinHttpReceiveResponse(request, None):
            raise ctypes.WinError(ctypes.get_last_error())
        check_request_state()

        status = dword()
        status_size = dword(ctypes.sizeof(status))
        status_index = dword()
        if not winhttp.WinHttpQueryHeaders(
                request, 19 | 0x20000000, None, ctypes.byref(status),
                ctypes.byref(status_size), ctypes.byref(status_index)):
            raise ctypes.WinError(ctypes.get_last_error())
        check_request_state()

        chunks = []
        total = 0
        while True:
            check_request_state()
            available = dword()
            if not winhttp.WinHttpQueryDataAvailable(
                    request, ctypes.byref(available)):
                raise ctypes.WinError(ctypes.get_last_error())
            check_request_state()
            if available.value == 0:
                break
            if int(available.value) > MAX_RESPONSE_BYTES - total:
                raise ReplayLabRestoreClientError(
                    "Bread Host returned an invalid response size")
            request_bytes = min(16 * 1024, int(available.value))
            # Read only bytes WinHTTP has already made available. Asking for the
            # entire remaining Content-Length lets a slow peer keep one
            # synchronous ReadData call alive indefinitely by dripping bytes.
            buffer = ctypes.create_string_buffer(request_bytes)
            received = dword()
            if not winhttp.WinHttpReadData(
                    request, buffer, request_bytes, ctypes.byref(received)):
                raise ctypes.WinError(ctypes.get_last_error())
            check_request_state()
            if received.value == 0:
                break
            total += int(received.value)
            chunks.append(buffer.raw[:received.value])
        return int(status.value), b"".join(chunks)
    except ReplayLabRestoreClientError:
        raise
    except (OSError, ValueError) as exc:
        if cancel_event.is_set():
            raise ReplayLabRestoreClientError(
                "Bread project restore was cancelled during shutdown")
        if method == "POST" and request_started:
            raise ReplayLabRestoreRequestUncertain(
                "Bread Host request outcome is uncertain: %s" % str(exc))
        raise ReplayLabRestoreTransportError(exc, method, path) from exc
    finally:
        for handle in (request, connection, session):
            if handle:
                try:
                    winhttp.WinHttpCloseHandle(handle)
                except Exception:
                    pass


def _winhttp_transport(port, token, method, path, body, timeout_seconds,
                       cancel_event, expected_generation):
    headers = (
        "Authorization: Bearer %s\r\n"
        "Accept: application/json\r\n" % token)
    if body:
        headers += "Content-Type: application/json; charset=utf-8\r\n"
    return _winhttp_request_blocking(
        method, path, port, headers, body, timeout_seconds, cancel_event,
        expected_generation)


def _winhttp_health_transport(port, timeout_seconds, cancel_event,
                              expected_generation):
    """Read the public Host identity before sending a bearer credential."""

    return _winhttp_request_blocking(
        "GET", "/health", port, "Accept: application/json\r\n", b"",
        timeout_seconds, cancel_event, expected_generation)


def begin_restore_shutdown():
    """Install a sticky fence and cancel every restore registered before it."""

    global _RESTORE_GENERATION, _RESTORE_STOPPING
    with _RESTORE_LIFECYCLE:
        if not _RESTORE_STOPPING:
            _RESTORE_GENERATION += 1
            _RESTORE_STOPPING = True
        generation = _RESTORE_GENERATION
        clients = list(_ACTIVE_CLIENTS)
    for client in clients:
        try:
            client.cancel()
        except Exception:
            pass
    return generation


def cancel_active_restores():
    """Backward-compatible name for the sticky launcher shutdown fence."""

    return begin_restore_shutdown()


def resume_restore_generation():
    """Open a fresh lifecycle generation before constructing a new executor."""

    global _RESTORE_GENERATION, _RESTORE_STOPPING
    with _RESTORE_LIFECYCLE:
        if _ACTIVE_CLIENTS:
            raise ReplayLabRestoreClientError(
                "Bread project restore workers are still active")
        _RESTORE_GENERATION += 1
        _RESTORE_STOPPING = False
        return _RESTORE_GENERATION


def wait_restore_quiescent(generation, timeout_seconds):
    """Wait until the fenced generation owns no Python restore workers."""

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    with _RESTORE_LIFECYCLE:
        if generation != _RESTORE_GENERATION or not _RESTORE_STOPPING:
            return False
        while _ACTIVE_CLIENTS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _RESTORE_LIFECYCLE.wait(remaining)
        return True


def runtime_root(environment=None):
    """Resolve the private service directory, separate from ReplayLabMCP."""

    env = os.environ if environment is None else environment
    configured = str(
        env.get("REPLAYLAB_INTERNAL_RUNTIME_DIR", "") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    local = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local:
        return os.path.abspath(os.path.join(
            os.path.expanduser(local), "ReplayLab", "internal-runtime"))
    getuid = getattr(os, "getuid", None)
    suffix = str(getuid() if callable(getuid) else os.getpid())
    return os.path.abspath(os.path.join(
        tempfile.gettempdir(), "ReplayLabInternal-" + suffix))


def resolve_internal_executable(hint=None):
    """Resolve only the first-party internal executable from fixed locations."""

    candidates = []
    supplied = str(hint or "").strip()
    if supplied:
        absolute = os.path.abspath(os.path.expanduser(supplied))
        if os.path.basename(absolute).lower() == "replaylabinternal.exe":
            candidates.append(absolute)
        else:
            candidates.append(os.path.join(
                os.path.dirname(absolute), "ReplayLabInternal.exe"))
    executable_root = os.path.dirname(os.path.abspath(sys.executable))
    candidates.extend([
        os.path.join(executable_root, "replaylab", "ReplayLabInternal.exe"),
        os.path.join(executable_root, "ReplayLabInternal.exe"),
    ])
    seen = set()
    for candidate in candidates:
        normal = os.path.normcase(os.path.abspath(candidate))
        if normal in seen:
            continue
        seen.add(normal)
        if os.path.isfile(candidate) and not _is_reparse(candidate):
            return os.path.abspath(candidate)
    return ""


def _is_reparse(path):
    if os.path.islink(path):
        return True
    if os.name != "nt":
        return False
    try:
        get_attributes = ctypes.windll.kernel32.GetFileAttributesW
        get_attributes.argtypes = (ctypes.c_wchar_p,)
        get_attributes.restype = ctypes.c_uint32
        attributes = int(get_attributes(str(path)))
        return attributes != 0xFFFFFFFF and bool(attributes & 0x400)
    except Exception:
        return True


def _read_bounded(path, maximum, label):
    value_path = os.path.abspath(str(path))
    if _is_reparse(value_path) or not os.path.isfile(value_path):
        raise ReplayLabRestoreClientError("%s is unavailable" % label)
    try:
        size = int(os.path.getsize(value_path))
    except (OSError, TypeError, ValueError):
        raise ReplayLabRestoreClientError("%s is unavailable" % label)
    if size <= 0 or size > maximum:
        raise ReplayLabRestoreClientError("%s is invalid" % label)
    try:
        with open(value_path, "rb") as stream:
            value = stream.read(maximum + 1)
    except OSError as exc:
        raise ReplayLabRestoreClientError(
            "%s could not be read: %s" % (label, str(exc)))
    if not value or len(value) != size or len(value) > maximum:
        raise ReplayLabRestoreClientError("%s changed while it was read" % label)
    return value


def _process_alive(pid):
    try:
        value = int(pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if value <= 0:
        return False
    if value == os.getpid():
        return True
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (
            ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, value)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_uint32()
            return bool(kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:  # pragma: no cover - qrenderdoc's packaged Host is Windows-only.
        os.kill(value, 0)
        return True
    except OSError:
        return False


def _strict_json_bytes(payload, label):
    def _reject_duplicate(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def _reject_constant(value):
        raise ValueError("non-finite number: %s" % value)

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate,
            parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ReplayLabRestoreClientError(
            "%s returned invalid JSON: %s" % (label, str(exc)))
    if not isinstance(value, dict):
        raise ReplayLabRestoreClientError("%s returned a non-object" % label)
    return value


def _bounded_integer(value, label, minimum=0, maximum=MAX_PROGRESS_COUNT):
    if (isinstance(value, bool) or not isinstance(value, int) or
            value < minimum or value > maximum):
        raise ReplayLabRestoreClientError("%s is invalid" % label)
    return value


def _bounded_text(value, label, maximum=4096, allow_empty=False):
    if not isinstance(value, str):
        raise ReplayLabRestoreClientError("%s must be text" % label)
    text = value
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise ReplayLabRestoreClientError("%s is invalid" % label)
    if ((not text and not allow_empty) or len(encoded) > maximum or
            any(ord(character) < 0x20 for character in text)):
        raise ReplayLabRestoreClientError("%s is invalid" % label)
    return text


def _bounded_diagnostic(value, label, maximum=MAX_RESPONSE_BYTES,
                        allow_empty=False):
    """Validate producer diagnostics without flattening compiler output."""
    if not isinstance(value, str):
        raise ReplayLabRestoreClientError("%s must be text" % label)
    text = value
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise ReplayLabRestoreClientError("%s is invalid" % label)
    if ((not text and not allow_empty) or len(encoded) > maximum or
            any((ord(character) < 0x20 and character not in "\t\r\n") or
                0x7F <= ord(character) < 0xA0
                for character in text)):
        raise ReplayLabRestoreClientError("%s is invalid" % label)
    return text


def _hex_text(value, pattern, label):
    text = _bounded_text(value, label, 128).lower()
    if pattern.fullmatch(text) is None or text != value:
        raise ReplayLabRestoreClientError("%s is invalid" % label)
    return text


def _clean_path(value, label, require_rdc=False):
    if not isinstance(value, str):
        raise ReplayLabRestoreClientError("%s must be text" % label)
    text = value.strip()
    if not text or len(text) > 32768 or "\x00" in text:
        raise ReplayLabRestoreClientError("%s is invalid" % label)
    path = os.path.abspath(os.path.expanduser(text))
    if require_rdc and not path.lower().endswith(".rdc"):
        raise ReplayLabRestoreClientError("capture path must reference an RDC file")
    return path


def _normalise_progress(value, previous_sequence):
    if not isinstance(value, dict):
        raise ReplayLabRestoreClientError("Bread restore progress is malformed")
    legacy_fields = {"sequence", "phase", "message", "current", "total"}
    if set(value) not in (legacy_fields, legacy_fields | {"item"}):
        raise ReplayLabRestoreClientError("Bread restore progress is malformed")
    try:
        sequence = _bounded_integer(
            value["sequence"], "restore progress sequence", 1, 0x7FFFFFFF)
        current = _bounded_integer(
            value["current"], "restore progress current")
        total = _bounded_integer(
            value["total"], "restore progress total")
        phase = _bounded_text(value["phase"], "restore progress phase", 128)
        message = _bounded_text(
            value["message"], "restore progress message", 1024,
            allow_empty=True)
        item = _bounded_text(
            value.get("item", ""), "restore progress item", 1024,
            allow_empty=True)
    except ReplayLabRestoreClientError:
        raise ReplayLabRestoreClientError("Bread restore progress is malformed")
    if sequence <= previous_sequence or (total and current > total):
        raise ReplayLabRestoreClientError("Bread restore progress is malformed")
    return {
        "sequence": sequence,
        "phase": phase,
        "message": message,
        "current": current,
        "total": total,
        "item": item,
    }


def _validate_shader_items(value):
    items = value.get("items")
    if not isinstance(items, list) or len(items) > MAX_RESULT_ITEMS:
        raise ReplayLabRestoreClientError(
            "Bread restore Shader summary is malformed")
    counts = {
        "applied": 0,
        "skipped": 0,
        "failed": 0,
        "adoptedCurrentSession": 0,
        "reusedValidation": 0,
        "manualEdit": 0,
    }
    for item in items:
        if not isinstance(item, dict):
            raise ReplayLabRestoreClientError(
                "Bread restore Shader item is malformed")
        status = item.get("status")
        source = item.get("source")
        if (status not in ("applied", "skipped", "failed") or
                source not in ("rdc-index", "reuse-plan", "verified-rdc-restore") or
                (source == "verified-rdc-restore" and status != "applied")):
            raise ReplayLabRestoreClientError(
                "Bread restore Shader item is malformed")
        optional = set()
        if status == "applied":
            expected = {
                "status", "source", "workspacePath", "workspaceId",
                "resourceId", "stage", "replacementId",
            }
            optional.update({"reusedValidation", "pairValidationCache", "pendingSource", "preservedLocalSource",
                             "sourceSha256"})
            if source == "verified-rdc-restore":
                expected.update({"knowledgeId", "reusedValidation"})
            else:
                expected.add("idempotentlyAdopted")
            if source == "reuse-plan":
                expected.add("knowledgeId")
                # The project version replaced a local edit (backup receipt).
                optional.update({"supersededLocalEdit", "localValidatedCandidate"})
                optional.update({"sourceSha256", "candidateRawSha256", "candidateRevision",
                                 "projectSourceSha256", "projectCandidateRawSha256",
                                 "sourcePreparationOnly"})
        elif status == "failed":
            expected = {
                "status", "source", "resourceId", "stage", "error",
            }
            expected.add("workspacePath" if source == "rdc-index" else
                         "knowledgeId")
        else:
            expected = {
                "status", "source", "resourceId", "stage", "reason",
            }
            if source != "reuse-plan":
                raise ReplayLabRestoreClientError(
                    "Bread restore Shader item is malformed")
            optional |= {"detail", "knowledgeId", "knowledgeSourceSha256", "rawSha256",
                         "workspacePath", "workspaceSourceSha256"}
        present = set(item)
        if not expected <= present or not present <= (expected | optional):
            raise ReplayLabRestoreClientError(
                "Bread restore Shader item is malformed")
        if status == "applied":
            if "sourcePreparationOnly" in item and not isinstance(item["sourcePreparationOnly"], bool):
                raise ReplayLabRestoreClientError("Bread restore source preparation flag is malformed")
            if "candidateRevision" in item:
                _bounded_integer(item["candidateRevision"], "Shader candidate revision", 1, 0x7FFFFFFF)
            for field in ("sourceSha256", "candidateRawSha256", "projectSourceSha256"):
                if field in item:
                    _hex_text(item[field], SHA256_PATTERN, "Shader " + field)
            if "projectCandidateRawSha256" in item and item["projectCandidateRawSha256"] != "":
                _hex_text(item["projectCandidateRawSha256"], SHA256_PATTERN, "Shader project candidate")
            if (item.get("localValidatedCandidate") is False
                    and "sourceSha256" in item and "projectSourceSha256" in item
                    and item["sourceSha256"] != item["projectSourceSha256"]):
                raise ReplayLabRestoreClientError("Bread restore applied source differs from project source")
            if ("localValidatedCandidate" in item
                    and not isinstance(item["localValidatedCandidate"], bool)):
                raise ReplayLabRestoreClientError(
                    "Bread restore local Shader candidate flag is malformed")
            if source == "verified-rdc-restore":
                if item["reusedValidation"] is not True:
                    raise ReplayLabRestoreClientError(
                        "Bread restore Shader item is malformed")
            elif not isinstance(item["idempotentlyAdopted"], bool):
                raise ReplayLabRestoreClientError(
                    "Bread restore Shader item is malformed")
            elif item["idempotentlyAdopted"]:
                counts["adoptedCurrentSession"] += 1
            if "reusedValidation" in item:
                if not isinstance(item["reusedValidation"], bool):
                    raise ReplayLabRestoreClientError(
                        "Bread restore Shader reused validation flag is malformed")
                if item["reusedValidation"]:
                    counts["reusedValidation"] += 1
            pair_cache = item.get("pairValidationCache")
            manual_cache = (isinstance(pair_cache, dict) and
                            pair_cache.get("reason") == "manual-edit-unvalidated")
            if manual_cache:
                if (set(pair_cache) != {"hit", "written", "reason"} or
                        pair_cache["hit"] is not False or pair_cache["written"] is not False or
                        source != "reuse-plan" or item.get("reusedValidation") is not False or
                        item.get("idempotentlyAdopted") is not False or
                        not {"sourceSha256", "candidateRawSha256", "candidateRevision"} <= set(item)):
                    raise ReplayLabRestoreClientError(
                        "Bread restore manual Shader evidence is inconsistent")
                counts["manualEdit"] += 1
            elif pair_cache is not None and (
                    not isinstance(pair_cache, dict) or
                    set(pair_cache) != {"hit", "written"} or
                    any(not isinstance(pair_cache[field], bool)
                        for field in ("hit", "written"))):
                raise ReplayLabRestoreClientError(
                    "Bread restore Shader pair cache flags are malformed")
            superseded = item.get("supersededLocalEdit")
            if superseded is not None:
                if not isinstance(superseded, dict) or not isinstance(
                        superseded.get("backupPath"), str):
                    raise ReplayLabRestoreClientError(
                        "Bread restore Shader item is malformed")
                _clean_path(superseded["backupPath"], "Shader local-edit backup path")
                counts["supersededLocalEdits"] = counts.get("supersededLocalEdits", 0) + 1
        _bounded_integer(
            item["resourceId"], "Shader resource ID", 1,
            0x7FFFFFFFFFFFFFFF)
        _bounded_text(item["stage"], "Shader stage", 32)
        for field in ("workspaceId", "replacementId", "knowledgeId"):
            if field in item:
                _hex_text(item[field], JOB_ID_PATTERN, "Shader " + field)
        if "workspacePath" in item:
            _clean_path(item["workspacePath"], "Shader workspace path")
        for field in ("error", "reason", "detail"):
            if field in item:
                _bounded_diagnostic(item[field], "Shader " + field)
        for field in ("knowledgeSourceSha256", "rawSha256", "workspaceSourceSha256"):
            if item.get(field):
                _hex_text(item[field], SHA256_PATTERN, "Shader " + field)
        counts[status] += 1
    _validate_pending_source_publications(items)
    _validate_preserved_local_sources(items)
    return items, counts


_SOURCE_ONLY_REASONS = frozenset({
    "source-only-project-draft", "legacy-saved-organization-draft",
    "legacy-organization-publication-needs-review",
})


def _validate_preserved_local_sources(items):
    """A local working copy is display evidence, never another replay receipt."""
    applied_ids = {item.get("workspaceId") for item in items if item["status"] == "applied"}
    applied_paths = {os.path.normcase(_clean_path(item["workspacePath"], "applied Shader workspace"))
                     for item in items if item["status"] == "applied"}
    seen = {}
    for item in items:
        if "preservedLocalSource" not in item:
            continue
        source = item["preservedLocalSource"]
        if (item["status"] != "applied" or not isinstance(source, dict)
                or set(source) != {"workspaceId", "workspacePath", "sourceSha256", "shown", "reason"}
                or source.get("reason") != "local-edit" or type(source.get("shown")) is not bool):
            raise ReplayLabRestoreClientError("Bread restore preserved local Shader source is malformed")
        workspace_id = _hex_text(source["workspaceId"], JOB_ID_PATTERN, "local Shader workspace ID")
        source_sha = _hex_text(source["sourceSha256"], SHA256_PATTERN, "local Shader source hash")
        path = _clean_path(source["workspacePath"], "local Shader workspace path")
        if (not os.path.isabs(source["workspacePath"]) or workspace_id in applied_ids
                or os.path.normcase(path) in applied_paths):
            raise ReplayLabRestoreClientError("Bread restore local Shader source overlaps a replay workspace")
        identity = (os.path.normcase(path), source_sha)
        if workspace_id in seen and seen[workspace_id] != identity:
            raise ReplayLabRestoreClientError("Bread restore local Shader source identity is inconsistent")
        seen[workspace_id] = identity


def _validate_pending_source_publications(items):
    """Keep the current editable head distinct from the applied validated head.

    A pending source annotates one applied row; it never adds an application,
    validation receipt, or skipped item to the restore accounting.
    """
    applied_knowledge = {item.get("knowledgeId") for item in items
                         if item["status"] == "applied"}
    seen_resources = set()
    seen_knowledge = {}
    for item in items:
        pending = item.get("pendingSource")
        if pending is None and "pendingSource" not in item:
            continue
        required = {"knowledgeId", "sourceSha256", "reason", "shown"}
        if (item["status"] != "applied" or not isinstance(pending, dict)
                or not required <= set(pending)
                or not set(pending) <= required | {"workspacePath"}
                or pending.get("reason") not in _SOURCE_ONLY_REASONS
                or type(pending.get("shown")) is not bool):
            raise ReplayLabRestoreClientError("Bread restore pending Shader source is malformed")
        _hex_text(item.get("knowledgeId"), JOB_ID_PATTERN, "applied Shader knowledge ID")
        _hex_text(item.get("sourceSha256"), SHA256_PATTERN, "applied Shader source hash")
        knowledge_id = _hex_text(pending["knowledgeId"], JOB_ID_PATTERN,
                                 "pending Shader knowledge ID")
        source_sha = _hex_text(pending["sourceSha256"], SHA256_PATTERN, "pending Shader source hash")
        if "workspacePath" in pending:
            path = _clean_path(pending["workspacePath"], "pending Shader workspace path")
            if not os.path.isabs(pending["workspacePath"]) or not path:
                raise ReplayLabRestoreClientError("Bread restore pending Shader workspace path is invalid")
        elif pending["shown"]:
            raise ReplayLabRestoreClientError("Bread restore shown pending Shader source has no workspace")
        identity = (item["resourceId"], item["stage"])
        if (identity in seen_resources
                or (knowledge_id in seen_knowledge and seen_knowledge[knowledge_id] != source_sha)
                or knowledge_id in applied_knowledge):
            raise ReplayLabRestoreClientError("Bread restore pending Shader identity is duplicated")
        seen_resources.add(identity)
        seen_knowledge[knowledge_id] = source_sha
    for item in items:
        if "pendingSource" not in item and (
                (item["resourceId"], item["stage"]) in seen_resources
                or item.get("knowledgeId") in seen_knowledge):
            raise ReplayLabRestoreClientError("Bread restore pending Shader source is counted more than once")


def _validated_source_only_skips(items):
    """Count editor-only publications without treating them as replay receipts."""
    reasons = _SOURCE_ONLY_REASONS
    applied_resources = {(item["resourceId"], item["stage"]) for item in items
                         if item["status"] == "applied"}
    applied_knowledge = {item.get("knowledgeId") for item in items
                         if item["status"] == "applied"}
    seen = set()
    count = 0
    for item in items:
        if item["status"] != "skipped" or item.get("reason") not in reasons:
            continue
        knowledge_id = _hex_text(item.get("knowledgeId"), JOB_ID_PATTERN,
                                 "source-only Shader knowledge ID")
        source_sha = _hex_text(item.get("knowledgeSourceSha256"), SHA256_PATTERN,
                              "source-only Shader source hash")
        identity = (item["resourceId"], item["stage"])
        if (identity in seen or identity in applied_resources or knowledge_id in applied_knowledge
                or ("workspacePath" in item) != ("workspaceSourceSha256" in item)
                or ("workspaceSourceSha256" in item and item["workspaceSourceSha256"] != source_sha)):
            raise ReplayLabRestoreClientError("Bread restore source-only Shader identity is inconsistent")
        seen.add(identity)
        count += 1
    return count


def _validate_restore_target_lookup(value):
    if value.get("inventoryScope") != "target-raw-sha256" or value.get("fullFrame") is not False:
        raise ReplayLabRestoreClientError("target Shader restore cannot claim full-frame coverage")
    lookup = value.get("targetLookup")
    if not isinstance(lookup, dict) or set(lookup) != {
            "targetRawSha256s", "observedTargetRawSha256s", "unobservedTargetRawSha256s",
            "targetInventoryComplete", "rawIdentitySearchComplete"}:
        raise ReplayLabRestoreClientError("target Shader restore lookup is invalid")
    lists = []
    for name in ("targetRawSha256s", "observedTargetRawSha256s", "unobservedTargetRawSha256s"):
        values = lookup[name]
        if not isinstance(values, list) or len(values) > MAX_RESULT_ITEMS:
            raise ReplayLabRestoreClientError("target Shader restore hashes are invalid")
        clean = [_hex_text(item, SHA256_PATTERN, name) for item in values]
        if len(set(clean)) != len(clean):
            raise ReplayLabRestoreClientError("target Shader restore hashes are duplicated")
        lists.append(set(clean))
    targets, observed, unobserved = lists
    if not targets or observed & unobserved or observed | unobserved != targets:
        raise ReplayLabRestoreClientError("target Shader restore hash coverage is inconsistent")
    for name in ("targetInventoryComplete", "rawIdentitySearchComplete"):
        if not isinstance(lookup[name], bool) or (value["completed"] and lookup[name] is not True):
            raise ReplayLabRestoreClientError("incomplete target Shader lookup cannot report completion")


def _validate_restore_editing(value, shader_items, capture_path, capture_session_id):
    if isinstance(value, dict) and value.get("status") == "pending":
        if (set(value) != {"status", "pending", "attempted", "enabled", "deferred", "failed", "items"}
                or any(value.get(key) != 0 or type(value.get(key)) is not int
                       for key in ("attempted", "enabled", "deferred", "failed"))):
            raise ReplayLabRestoreClientError("pending Shader editor summary is invalid")
        pending = _bounded_integer(value["pending"], "pending editors", 0, MAX_RESULT_ITEMS)
        # Reuse the exact workspace/capture validator without granting a live
        # watcher ID or claiming that an editor has already started.
        normalized = {"attempted": pending, "enabled": 0, "deferred": 0, "failed": pending, "items": []}
        if not isinstance(value["items"], list) or len(value["items"]) != pending:
            raise ReplayLabRestoreClientError("pending Shader editor counts are inconsistent")
        for item in value["items"]:
            if (not isinstance(item, dict) or set(item) != {"workspaceId", "workspacePath", "status", "captureBinding"}
                    or item.get("status") != "pending"):
                raise ReplayLabRestoreClientError("pending Shader editor item is invalid")
            binding = item["captureBinding"]
            if (not isinstance(binding, dict) or set(binding) != {"instanceId", "captureSessionId", "capturePath"}
                    or not isinstance(binding["instanceId"], str)
                    or re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", binding["instanceId"]) is None
                    or binding["instanceId"] in (".", "..")
                    or binding["captureSessionId"] != capture_session_id
                    or os.path.normcase(_clean_path(binding["capturePath"], "editor capture path", require_rdc=True))
                       != os.path.normcase(capture_path)):
                raise ReplayLabRestoreClientError("pending Shader editor belongs to another capture")
            normalized["items"].append({"workspaceId": item["workspaceId"], "workspacePath": item["workspacePath"],
                                        "status": "failed", "error": "editor startup pending"})
        _validate_restore_editing(normalized, shader_items, capture_path, capture_session_id)
        return
    if not isinstance(value, dict):
        raise ReplayLabRestoreClientError("restored Shader editor summary is invalid")
    diagnostic_keys = {"initializationMs", "startupReuse", "ready"} & set(value)
    if "initializationMs" in value and (type(value["initializationMs"]) not in (int, float)
            or not 0 <= value["initializationMs"] <= 86400000):
        raise ReplayLabRestoreClientError("restored Shader editor timing values are invalid")
    if "startupReuse" in value:
        reuse = value["startupReuse"]
        if (not isinstance(reuse, dict) or len(reuse) > 16
                or any(not isinstance(key, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", key) is None
                       or type(count) is not int or not 0 <= count <= 1000000000
                       for key, count in reuse.items())):
            raise ReplayLabRestoreClientError("restored Shader editor timing values are invalid")
    if (set(value) - diagnostic_keys not in (
            {"attempted", "enabled", "failed", "items"},
            {"attempted", "enabled", "deferred", "failed", "items"})):
        raise ReplayLabRestoreClientError("restored Shader editor summary is invalid")
    counts = {name: _bounded_integer(value[name], "editor " + name, 0, MAX_RESULT_ITEMS)
              for name in ("attempted", "enabled", "failed")}
    counts["deferred"] = _bounded_integer(value.get("deferred", 0), "editor deferred", 0, MAX_RESULT_ITEMS)
    counts["ready"] = _bounded_integer(value.get("ready", 0), "editor ready", 0, MAX_RESULT_ITEMS)
    applied = {item["workspaceId"]: item for item in shader_items if item.get("status") == "applied"}
    items = value["items"]
    if (not isinstance(items, list) or len(items) != counts["attempted"]
            or counts["attempted"] != len(applied)
            or counts["attempted"] != counts["enabled"] + counts["deferred"] + counts["ready"] + counts["failed"]):
        raise ReplayLabRestoreClientError("restored Shader editor counts are inconsistent")
    seen, enabled, deferred, ready = set(), 0, 0, 0
    for item in items:
        if not isinstance(item, dict) or item.get("status") not in ("enabled", "deferred", "ready", "failed"):
            raise ReplayLabRestoreClientError("restored Shader editor item is invalid")
        expected = {"workspaceId", "workspacePath", "status"}
        if item["status"] == "enabled":
            expected.update({"liveEditId", "watchId", "captureBinding"})
        elif item["status"] == "ready":
            expected.update({"editingMode", "readOnly", "backgroundAutoApplyAuthorized", "captureBinding"})
        elif item["status"] == "deferred":
            expected.update({"reason", "editingMode", "readOnly",
                             "backgroundAutoApplyAuthorized", "captureBinding"})
            if item.get("reason") != "local-edit-preserved":
                expected.add("organizationId")
        else:
            expected.add("error")
        if "displayAliases" in item:
            expected.add("displayAliases")
            display = item["displayAliases"]
            if (not isinstance(display, dict) or not set(display).issubset(
                    {"status", "writtenRows", "writtenBlocks", "skipped", "diagnostic", "reason", "metadataCacheHit"})
                    or display.get("status") not in {"not-required", "unavailable", "current", "restored", "partial", "failed"}
                    or any(type(display.get(key, 0)) is not int or not 0 <= display.get(key, 0) <= 65536
                           for key in ("writtenRows", "writtenBlocks"))
                    or any(not isinstance(display.get(key, ""), str) or len(display.get(key, "")) > 400
                           for key in ("diagnostic", "reason"))
                    or not isinstance(display.get("skipped", []), list)
                    or len(display.get("skipped", [])) > 64
                    or ("metadataCacheHit" in display and type(display["metadataCacheHit"]) is not bool)
                    or len(json.dumps(display, ensure_ascii=False)) > 65536):
                raise ReplayLabRestoreClientError("restored CBuffer display aliases are invalid")
        if "startTiming" in item:
            expected.add("startTiming")
            timing = item["startTiming"]
            if not isinstance(timing, dict) or set(timing) != {"totalMs", "phasesMs", "failedPhase"}:
                raise ReplayLabRestoreClientError("restored Shader editor timing is invalid")
            total = timing["totalMs"]
            phases = timing["phasesMs"]
            if (type(total) not in (int, float) or not 0 <= total <= 86400000
                    or not isinstance(phases, dict) or len(phases) > 16
                    or any(not isinstance(name, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", name) is None
                           or type(elapsed) not in (int, float) or not 0 <= elapsed <= total + 1
                           for name, elapsed in phases.items())
                    or not isinstance(timing["failedPhase"], str)
                    or timing["failedPhase"] and timing["failedPhase"] not in phases):
                raise ReplayLabRestoreClientError("restored Shader editor timing values are invalid")
        if "displayAliasElapsedMs" in item:
            expected.add("displayAliasElapsedMs")
            elapsed = item["displayAliasElapsedMs"]
            if type(elapsed) not in (int, float) or not 0 <= elapsed <= 86400000:
                raise ReplayLabRestoreClientError("restored Shader editor timing values are invalid")
        if set(item) != expected:
            raise ReplayLabRestoreClientError("restored Shader editor fields are invalid")
        workspace_id = _hex_text(item["workspaceId"], JOB_ID_PATTERN, "editor workspace ID")
        if workspace_id not in applied or workspace_id in seen:
            raise ReplayLabRestoreClientError("restored Shader editor has no unique applied workspace")
        seen.add(workspace_id)
        path = _clean_path(item["workspacePath"], "editor workspace path")
        if os.path.normcase(path) != os.path.normcase(applied[workspace_id]["workspacePath"]):
            raise ReplayLabRestoreClientError("restored Shader editor workspace changed")
        if item["status"] == "failed":
            _bounded_diagnostic(item["error"], "editor error")
            continue
        if item["status"] == "ready":
            ready += 1
            if (item["editingMode"] != "on-demand" or item["readOnly"] is not False
                    or item["backgroundAutoApplyAuthorized"] is not False or "startTiming" in item):
                raise ReplayLabRestoreClientError("prepared Shader editing cannot authorize background monitoring")
        elif item["status"] == "deferred":
            deferred += 1
            local_edit = item["reason"] == "local-edit-preserved"
            if not local_edit:
                _hex_text(item["organizationId"], JOB_ID_PATTERN, "editor organization ID")
            if (item["reason"] not in {"organization-draft-open", "local-edit-preserved"}
                    or item["editingMode"] != "source-only"
                    or item["readOnly"] is not (False if local_edit else True)
                    or (local_edit and "preservedLocalSource" not in applied[workspace_id])
                    or item["backgroundAutoApplyAuthorized"] is not False
                    or "startTiming" in item):
                raise ReplayLabRestoreClientError("deferred Shader editing cannot authorize monitoring or source writes")
        else:
            enabled += 1
            for name in ("liveEditId", "watchId"):
                _hex_text(item[name], JOB_ID_PATTERN, "editor " + name)
        binding = item["captureBinding"]
        if not isinstance(binding, dict) or set(binding) != {"instanceId", "captureSessionId", "capturePath"}:
            raise ReplayLabRestoreClientError("restored Shader editor capture binding is invalid")
        instance = _bounded_text(binding["instanceId"], "editor instance ID", 512)
        if (re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", instance) is None or instance in (".", "..")
                or binding["captureSessionId"] != capture_session_id
                or os.path.normcase(_clean_path(binding["capturePath"], "editor capture path", require_rdc=True))
                != os.path.normcase(capture_path)):
            raise ReplayLabRestoreClientError("restored Shader editor belongs to another capture")
    if enabled != counts["enabled"] or deferred != counts["deferred"] or ready != counts["ready"]:
        raise ReplayLabRestoreClientError("restored Shader editor states contradict counts")


def _validate_material_items(value):
    items = value.get("items")
    if not isinstance(items, list) or len(items) > MAX_RESULT_ITEMS:
        raise ReplayLabRestoreClientError(
            "Bread restore material summary is malformed")
    counts = {"restored": 0, "skipped": 0, "failed": 0}
    restored_sessions = []
    for item in items:
        if not isinstance(item, dict):
            raise ReplayLabRestoreClientError(
                "Bread restore material item is malformed")
        status = item.get("status")
        source = item.get("source")
        if status == "restored":
            expected = {
                "status", "sessionId", "knowledgeId", "eventId", "stage",
                "blockIndex", "constantBlockPath", "parameterCount", "updates",
            }
            if source is not None:
                if source != "reuse-plan":
                    raise ReplayLabRestoreClientError(
                        "Bread restore material item is malformed")
                expected.add("source")
            if set(item) != expected:
                raise ReplayLabRestoreClientError(
                    "Bread restore material item is malformed")
            session_id = _hex_text(
                item["sessionId"], JOB_ID_PATTERN, "material session ID")
            restored_sessions.append(session_id)
            _hex_text(
                item["knowledgeId"], JOB_ID_PATTERN, "material knowledge ID")
            _bounded_integer(
                item["eventId"], "material event ID", 1,
                0x7FFFFFFFFFFFFFFF)
            _bounded_integer(
                item["blockIndex"], "material block index", 0,
                MAX_PROGRESS_COUNT)
            _bounded_text(item["stage"], "material stage", 32)
            _bounded_text(
                item["constantBlockPath"], "material CBuffer path", 2048)
            parameter_count = _bounded_integer(
                item["parameterCount"], "material parameter count", 0, 256)
            updates = item["updates"]
            if not isinstance(updates, list) or len(updates) != parameter_count:
                raise ReplayLabRestoreClientError(
                    "Bread restore material item is malformed")
            for update in updates:
                if (not isinstance(update, dict) or
                        set(update) != {"parameterId", "liveParameterId", "path"}):
                    raise ReplayLabRestoreClientError(
                        "Bread restore material item is malformed")
                _hex_text(
                    update["parameterId"], JOB_ID_PATTERN,
                    "material parameter ID")
                _hex_text(
                    update["liveParameterId"], JOB_ID_PATTERN,
                    "live material parameter ID")
                _bounded_text(
                    update["path"], "material parameter path", 2048)
        elif status == "failed":
            if source == "rdc-index":
                expected = {
                    "status", "source", "knowledgeId", "eventId", "stage",
                    "error",
                }
            elif source == "reuse-plan":
                expected = {
                    "status", "source", "knowledgeId", "resourceId", "stage",
                    "error",
                }
            else:
                raise ReplayLabRestoreClientError(
                    "Bread restore material item is malformed")
            if set(item) != expected:
                raise ReplayLabRestoreClientError(
                    "Bread restore material item is malformed")
            _hex_text(
                item["knowledgeId"], JOB_ID_PATTERN, "material knowledge ID")
            if "eventId" in item:
                _bounded_integer(
                    item["eventId"], "material event ID", 1,
                    0x7FFFFFFFFFFFFFFF)
            if "resourceId" in item:
                _bounded_integer(
                    item["resourceId"], "material resource ID", 1,
                    0x7FFFFFFFFFFFFFFF)
            _bounded_text(item["stage"], "material stage", 32)
            _bounded_diagnostic(item["error"], "material error")
        elif status == "skipped" and source == "reuse-plan":
            if set(item) != {
                    "status", "source", "resourceId", "stage", "reason"}:
                raise ReplayLabRestoreClientError(
                    "Bread restore material item is malformed")
            _bounded_integer(
                item["resourceId"], "material resource ID", 1,
                0x7FFFFFFFFFFFFFFF)
            _bounded_text(item["stage"], "material stage", 32)
            _bounded_diagnostic(item["reason"], "material reason")
        else:
            raise ReplayLabRestoreClientError(
                "Bread restore material item is malformed")
        counts[status] += 1
    return items, counts, restored_sessions


def _canonical_json_sha256(value):
    """Hash the small semantic restore result objects exactly like the Host."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ReplayLabRestoreClientError(
            "Bread restore desired head is malformed")
    return hashlib.sha256(encoded).hexdigest()


def _validate_restore_desired_head(value):
    """Validate the content-addressed project head exposed for diagnostics."""

    if value is None:
        return None
    fields = {
        "activeKnowledgeRevision", "knowledgeContentSha256",
        "knowledgeIdsSha256", "artifactRefsSha256",
        "referencedHeadsSha256", "headSha256", "complete",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ReplayLabRestoreClientError(
            "Bread restore desired head is malformed")
    try:
        _bounded_integer(
            value["activeKnowledgeRevision"],
            "restore active knowledge revision", 0, (1 << 63) - 1)
        for field in (
                "knowledgeContentSha256", "knowledgeIdsSha256",
                "artifactRefsSha256", "referencedHeadsSha256",
                "headSha256"):
            _hex_text(value[field], SHA256_PATTERN, "restore " + field)
    except ReplayLabRestoreClientError:
        raise ReplayLabRestoreClientError(
            "Bread restore desired head is malformed")
    if value.get("complete") is not True:
        raise ReplayLabRestoreClientError(
            "Bread restore desired head is malformed")
    semantic = {
        "knowledgeContentSha256": value["knowledgeContentSha256"],
        "knowledgeIdsSha256": value["knowledgeIdsSha256"],
        "artifactRefsSha256": value["artifactRefsSha256"],
        "referencedHeadsSha256": value["referencedHeadsSha256"],
        "complete": True,
    }
    if value["headSha256"] != _canonical_json_sha256(semantic):
        raise ReplayLabRestoreClientError(
            "Bread restore desired head is malformed")
    return value


def _validate_inventory_cache_summary(value):
    if (not isinstance(value, dict) or
            set(value) != {"hit", "written", "reason"} or
            not isinstance(value.get("hit"), bool) or
            not isinstance(value.get("written"), bool)):
        raise ReplayLabRestoreClientError(
            "Bread restore inventory cache summary is malformed")
    try:
        _bounded_diagnostic(value.get("reason"),
                            "restore inventory cache reason", 4096)
    except ReplayLabRestoreClientError:
        raise ReplayLabRestoreClientError(
            "Bread restore inventory cache summary is malformed")
    return value


def _validate_realized_receipt_summary(value):
    fields = {
        "loadedFromIndex", "validatedForRouting", "produced",
        "adoptedCurrentSession", "mutationsCreated",
        "idempotentApplyResponses", "persisted",
    }
    if (not isinstance(value, dict) or not fields <= set(value) or
            not set(value) <= fields | {"reusedValidation", "manualEdit"}):
        raise ReplayLabRestoreClientError(
            "Bread restore realized receipt summary is malformed")
    try:
        counts = {
            field: _bounded_integer(
                value[field], "restore realized receipt " + field,
                0, MAX_RESULT_ITEMS)
            for field in fields
        }
        counts["reusedValidation"] = _bounded_integer(
            value.get("reusedValidation", 0), "restore reused validation receipts",
            0, MAX_RESULT_ITEMS)
        counts["manualEdit"] = _bounded_integer(
            value.get("manualEdit", 0), "restore manual Shader observations",
            0, MAX_RESULT_ITEMS)
    except ReplayLabRestoreClientError:
        raise ReplayLabRestoreClientError(
            "Bread restore realized receipt summary is malformed")
    if (counts["validatedForRouting"] > counts["loadedFromIndex"] or
            counts["produced"] !=
            counts["adoptedCurrentSession"] +
            counts["mutationsCreated"] +
            counts["idempotentApplyResponses"] or
            counts["persisted"] > counts["produced"] or
            counts["reusedValidation"] + counts["manualEdit"] > counts["produced"]):
        raise ReplayLabRestoreClientError(
            "Bread restore realized receipt summary is malformed")
    return counts


class ReplayLabRestoreHostClient(object):
    """Start and poll one restore job on the private internal service."""

    def __init__(
            self, runtime_directory=None,
            timeout_seconds=DEFAULT_PROJECT_RESTORE_CLIENT_TIMEOUT_SECONDS,
                 startup_timeout_seconds=60, poll_seconds=0.1,
                 request_timeout_seconds=3.0, transport=None,
                 health_transport=None, sleep=None, monotonic=None,
                 internal_executable=None, confirm_replay_rollback=False):
        self.runtime_directory = os.path.abspath(
            runtime_directory or runtime_root())
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.startup_timeout_seconds = max(
            0.1, float(startup_timeout_seconds))
        self.poll_seconds = min(1.0, max(0.01, float(poll_seconds)))
        # Restore endpoints only submit jobs or return bounded snapshots. Keep
        # this below the launcher's five-second worker shutdown budget.
        self.request_timeout_seconds = min(
            4.0, max(0.1, float(request_timeout_seconds)))
        self.transport = transport or _winhttp_transport
        self.health_transport = health_transport or _winhttp_health_transport
        self.sleep = sleep
        self.monotonic = monotonic or time.monotonic
        self.internal_executable = resolve_internal_executable(
            internal_executable)
        self._cancelled = threading.Event()
        self._restore_generation = None
        self._confirm_replay_rollback = confirm_replay_rollback
        self._inspection_requested = threading.Event()
        self.replay_inspection_safe = True

    def cancel(self):
        self._cancelled.set()

    def cancel_for_inspection(self):
        self._inspection_requested.set()
        self.cancel()

    def _cancel_requested(self):
        with _RESTORE_LIFECYCLE:
            generation_changed = (
                self._restore_generation is not None and
                self._restore_generation != _RESTORE_GENERATION)
            stopping = _RESTORE_STOPPING
        return self._cancelled.is_set() or stopping or generation_changed

    def _check_cancelled(self):
        if self._cancel_requested():
            raise ReplayLabRestoreClientError(
                "Bread project restore was cancelled during shutdown")

    def _wait(self, seconds):
        self._check_cancelled()
        duration = max(0.0, float(seconds))
        if self.sleep is None:
            self._cancelled.wait(duration)
        else:
            self.sleep(duration)
        self._check_cancelled()

    def _credentials(self, deadline):
        state_path = os.path.join(
            self.runtime_directory, "internal-host.json")
        token_path = os.path.join(
            self.runtime_directory, "internal-host.token")
        last_error = None
        while self.monotonic() < deadline:
            try:
                state = _strict_json_bytes(
                    _read_bounded(
                        state_path, MAX_STATE_BYTES,
                        "Bread internal Host state"),
                    "Bread internal Host state")
                if (state.get("schema") != "replaylab-internal-host/v1" or
                        state.get("service") != "ReplayLabInternal" or
                        state.get("host") != "127.0.0.1"):
                    raise ReplayLabRestoreClientError(
                        "Bread internal Host state has an invalid identity")
                try:
                    port = _bounded_integer(
                        state.get("port"), "Bread Host port", 1024, 65535)
                    heartbeat = _bounded_integer(
                        state.get("heartbeatUnixMs"),
                        "Bread Host heartbeat", 1, 0x7FFFFFFFFFFFFFFF)
                    pid = _bounded_integer(
                        state.get("pid"), "Bread Host process ID", 1,
                        0x7FFFFFFF)
                    version = _bounded_text(
                        state.get("version"), "Bread Host version", 128)
                    endpoint = _bounded_text(
                        state.get("endpoint"), "Bread Host endpoint", 256)
                except ReplayLabRestoreClientError:
                    raise ReplayLabRestoreClientError(
                        "Bread Host state is invalid")
                heartbeat_age = int(time.time() * 1000) - heartbeat
                if (heartbeat_age < -2000 or
                        heartbeat_age > MAX_HOST_HEARTBEAT_AGE_MS or
                        endpoint != (
                            "http://127.0.0.1:%d/replaylab/internal" % port) or
                        not _process_alive(pid)):
                    raise ReplayLabRestoreClientError(
                        "Bread internal Host state is stale")
                protocol_version = state.get("hostStateProtocolVersion")
                if (not _host_runtime_version_compatible(
                        version, EXPECTED_HOST_VERSION) or
                        isinstance(protocol_version, bool) or
                        not isinstance(protocol_version, int) or
                        protocol_version != INTERNAL_HOST_STATE_PROTOCOL_VERSION or
                        not _has_required_capabilities(state.get("capabilities")) or
                        not _has_required_capability_versions(
                            state.get("capabilitySchema"),
                            state.get("capabilityVersions"))):
                    raise ReplayLabRestoreHostIncompatible(
                        "Bread internal Host runtime is incompatible "
                        "(expected %s protocol %d and %s capability versions, "
                        "received %s protocol %s capabilities %s)" %
                        (EXPECTED_HOST_VERSION,
                         INTERNAL_HOST_STATE_PROTOCOL_VERSION,
                         INTERNAL_HOST_CAPABILITY_SCHEMA,
                         version, str(protocol_version),
                         _capability_contract_description(
                             state.get("capabilitySchema"),
                             state.get("capabilityVersions"))))
                try:
                    host_instance_id = _hex_text(
                        state.get("hostInstanceId"), JOB_ID_PATTERN,
                        "Bread Host instance ID")
                except ReplayLabRestoreClientError:
                    raise ReplayLabRestoreHostIncompatible(
                        "Bread internal Host runtime does not publish a valid "
                        "instance identity")
                published_token = state.get("tokenFile")
                if (not isinstance(published_token, str) or
                        os.path.normcase(os.path.abspath(published_token)) !=
                        os.path.normcase(os.path.abspath(token_path))):
                    raise ReplayLabRestoreClientError(
                        "Bread internal Host token path is inconsistent")
                token = _read_bounded(
                    token_path, MAX_TOKEN_BYTES,
                    "Bread internal Host token").decode("ascii").strip()
                if TOKEN_PATTERN.fullmatch(token) is None:
                    raise ReplayLabRestoreClientError(
                        "Bread internal Host token is invalid")
                return port, token, pid, version, host_instance_id
            except ReplayLabRestoreHostIncompatible:
                # A fresh, live Host with an older or different contract cannot
                # become compatible by waiting for another heartbeat.
                raise
            except (ReplayLabRestoreClientError, UnicodeDecodeError) as exc:
                last_error = exc
                self._check_cancelled()
                self._wait(min(self.poll_seconds, 0.1))
        raise ReplayLabRestoreClientError(
            "Bread internal Host did not become ready: %s" %
            str(last_error or "internal Host state is unavailable"))

    def _verify_health(self, port, pid, version, host_instance_id):
        self._check_cancelled()
        try:
            status, response_body = self.health_transport(
                int(port), min(2.0, self.request_timeout_seconds),
                self._cancelled, self._restore_generation)
        except ReplayLabRestoreClientError:
            raise
        except Exception as exc:
            raise ReplayLabRestoreClientError(
                "Bread internal Host health check failed: %s" % str(exc))
        if not response_body or len(response_body) > MAX_STATE_BYTES:
            raise ReplayLabRestoreClientError(
                "Bread internal Host health response is invalid")
        value = _strict_json_bytes(
            response_body, "Bread internal Host health check")
        response_pid = value.get("pid")
        if (isinstance(status, bool) or not isinstance(status, int) or
                status != 200 or
                value.get("service") != "ReplayLabInternal" or
                value.get("transport") != "internal-http" or
                value.get("hostInstanceId") != host_instance_id or
                isinstance(response_pid, bool) or
                not isinstance(response_pid, int) or response_pid != pid):
            raise ReplayLabRestoreClientError(
                "Bread internal Host health identity is invalid")
        protocol_version = value.get("hostStateProtocolVersion")
        if (not _host_runtime_version_compatible(
                    value.get("version"), EXPECTED_HOST_VERSION) or
                not _host_runtime_version_compatible(
                    version, EXPECTED_HOST_VERSION) or
                value.get("version") != version or
                isinstance(protocol_version, bool) or
                not isinstance(protocol_version, int) or
                protocol_version != INTERNAL_HOST_STATE_PROTOCOL_VERSION or
                not _has_required_capabilities(value.get("capabilities")) or
                not _has_required_capability_versions(
                    value.get("capabilitySchema"),
                    value.get("capabilityVersions"))):
            raise ReplayLabRestoreHostIncompatible(
                "Bread internal Host health schema/capability contract is incompatible "
                "(received %s)" % _capability_contract_description(
                    value.get("capabilitySchema"),
                    value.get("capabilityVersions")))

    def _request(self, port, token, method, path, payload=None,
                 maximum_request_bytes=MAX_STATE_BYTES,
                 cancellation_cleanup=False):
        if not cancellation_cleanup:
            self._check_cancelled()
        body = b""
        if payload is not None:
            body = json.dumps(
                payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")
            if len(body) > maximum_request_bytes:
                raise ReplayLabRestoreClientError(
                    "Bread internal request is oversized")
        try:
            status, response_body = self.transport(
                int(port), token, method, path, body,
                self.request_timeout_seconds,
                (threading.Event()
                 if cancellation_cleanup else self._cancelled),
                (None if cancellation_cleanup else self._restore_generation))
        except ReplayLabRestoreClientError:
            raise
        except Exception as exc:
            raise ReplayLabRestoreClientError(
                "Bread internal Host connection failed: %s" % str(exc))
        if not response_body or len(response_body) > MAX_RESPONSE_BYTES:
            raise ReplayLabRestoreClientError(
                "Bread internal Host returned an invalid response size")
        if not cancellation_cleanup:
            self._check_cancelled()
        value = _strict_json_bytes(response_body, "Bread internal Host")
        if isinstance(status, bool) or not isinstance(status, int):
            raise ReplayLabRestoreClientError(
                "Bread internal Host returned an invalid HTTP status")
        if method == "POST":
            allowed_statuses = (200, 202)
        elif method == "DELETE":
            # A running job acknowledges cooperative cancellation while the
            # worker rolls back; 404 is also a successful cleanup outcome for
            # this best-effort path because no owned job remains to cancel.
            allowed_statuses = (200, 202, 404)
        else:
            allowed_statuses = (200,)
        if status not in allowed_statuses:
            error = value.get("error", "")
            if isinstance(error, dict):
                detail = str(error.get("message", "") or "")[:4096]
            else:
                detail = str(error or "")[:4096]
            raise ReplayLabRestoreClientError(
                "Bread internal Host rejected the request (HTTP %s): %s" %
                (status, detail))
        return value

    def _cancel_restore_job_best_effort(self, port, token, job_id):
        """Ask the Host to stop one validated job without masking its cause."""

        try:
            self._request(
                port, token, "DELETE", "%s/%s" % (RESTORE_JOB_PATH, job_id),
                cancellation_cleanup=True)
        except Exception:
            # Cancellation/timeout remains the authoritative client outcome.
            # The Host also owns a deadline, so a failed cleanup request cannot
            # justify replacing or delaying that original diagnostic.
            pass

    def _confirm_restore_rollback(self, port, token, job_id):
        """Wait for this job's Host rollback, rather than its DELETE acknowledgment."""
        path = "%s/%s" % (RESTORE_JOB_PATH, job_id)
        deadline = self.monotonic() + 20
        try:
            result = self._request(port, token, "DELETE", path, cancellation_cleanup=True)
            while True:
                _, status, _, _ = self._snapshot(result, job_id)
                if status in ("completed", "failed"):
                    # A completed job won the race before analysis started.
                    # Failed rollback must not be mistaken for clean cancellation.
                    self.replay_inspection_safe = (
                        status == "completed" or
                        result.get("error") == "Bread project restore was cancelled")
                    return
                if self.monotonic() >= deadline:
                    return
                (self.sleep or time.sleep)(self.poll_seconds)
                result = self._request(port, token, "GET", path + "?after=0",
                                       cancellation_cleanup=True)
        except ReplayLabRestoreClientError:
            # Keep the admission barrier closed if the Host cannot prove cleanup.
            return

    @staticmethod
    def _snapshot(value, expected_job_id=None, after_sequence=0):
        if (not isinstance(value, dict) or
                value.get("schema") != RESTORE_JOB_SCHEMA):
            raise ReplayLabRestoreClientError(
                "Bread host returned an invalid restore job")
        try:
            _bounded_integer(
                value.get("schemaVersion"), "restore job schema version", 1, 1)
            after = _bounded_integer(
                after_sequence, "restore progress cursor", 0, 0x7FFFFFFF)
            job_id = _hex_text(
                value.get("jobId"), JOB_ID_PATTERN, "restore job ID")
            status = _bounded_text(
                value.get("status"), "restore job status", 16)
            latest_sequence = _bounded_integer(
                value.get("latestSequence"), "restore latest sequence", 0,
                0x7FFFFFFF)
        except ReplayLabRestoreClientError:
            raise ReplayLabRestoreClientError(
                "Bread host returned an invalid restore job")
        if (status not in ("queued", "running", "completed", "failed") or
                (expected_job_id is not None and job_id != expected_job_id) or
                latest_sequence < after):
            raise ReplayLabRestoreClientError(
                "Bread host returned an invalid restore job")
        base_fields = {
            "schema", "schemaVersion", "jobId", "status", "latestSequence",
            "progress", "progressTruncated",
        }
        expected_fields = set(base_fields)
        if status == "completed":
            expected_fields.add("result")
            if not isinstance(value.get("result"), dict):
                raise ReplayLabRestoreClientError(
                    "Bread host returned an invalid restore job")
        elif status == "failed":
            expected_fields.add("error")
            try:
                _bounded_diagnostic(value.get("error"), "restore job error")
            except ReplayLabRestoreClientError:
                raise ReplayLabRestoreClientError(
                    "Bread host returned an invalid restore job")
        if set(value) != expected_fields:
            raise ReplayLabRestoreClientError(
                "Bread host returned an invalid restore job")
        progress = value.get("progress", [])
        if not isinstance(progress, list) or len(progress) > MAX_PROGRESS_EVENTS:
            raise ReplayLabRestoreClientError(
                "Bread host returned invalid restore progress")
        truncated = value.get("progressTruncated")
        if not isinstance(truncated, bool):
            raise ReplayLabRestoreClientError(
                "Bread host returned invalid restore progress")
        normalised = []
        previous = after
        for raw_event in progress:
            event = _normalise_progress(raw_event, previous)
            if event["sequence"] > latest_sequence:
                raise ReplayLabRestoreClientError(
                    "Bread host returned invalid restore progress")
            normalised.append(event)
            previous = event["sequence"]
        if normalised:
            first_sequence = normalised[0]["sequence"]
            if ((not truncated and first_sequence != after + 1) or
                    (truncated and first_sequence <= after + 1)):
                raise ReplayLabRestoreClientError(
                    "Bread host returned invalid restore progress")
        elif truncated or latest_sequence > after:
            raise ReplayLabRestoreClientError(
                "Bread host returned invalid restore progress")
        return job_id, status, normalised, latest_sequence

    @staticmethod
    def _result(value, project_root, capture_path, capture_session_id):
        if (not isinstance(value, dict) or
                value.get("schema") != "replaylab-project-restore/v1" or
                not isinstance(value.get("completed"), bool)):
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")
        try:
            _bounded_integer(
                value.get("schemaVersion"), "restore result schema version", 1, 1)
            _hex_text(
                value.get("operationId"), JOB_ID_PATTERN,
                "restore operation ID")
        except ReplayLabRestoreClientError:
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")
        capture = value.get("capture")
        project = value.get("project")
        if (not isinstance(capture, dict) or not isinstance(project, dict) or
                set(capture) != {"path", "sessionId", "captureId"} or
                set(project) != {"root", "workspaceId", "replayProjectId"}):
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")
        result_capture_path = _clean_path(
            capture.get("path"), "result capture path", require_rdc=True)
        result_project_root = _clean_path(
            project.get("root"), "result project root")
        if (os.path.normcase(result_capture_path) !=
                os.path.normcase(capture_path) or
                os.path.normcase(result_project_root) !=
                os.path.normcase(project_root) or
                capture.get("sessionId") != capture_session_id):
            raise ReplayLabRestoreClientError(
                "Bread restore result identity does not match the request")
        base_fields = {
            "schema", "schemaVersion", "operationId", "completed", "capture",
            "project", "restoreMode", "desiredHead", "cache",
            "inventoryCache", "shaders", "materials", "index",
            "realizedReceipts", "warnings",
        }
        if "performance" in value:
            performance = value["performance"]
            if (not isinstance(performance,dict) or set(performance) != {"elapsedMs","phasesMs","digestCache","filePreparationWorkers","replayWorkers"}
                    or type(performance.get("elapsedMs")) not in (int,float) or not 0 <= performance["elapsedMs"] <= 86400000
                    or not isinstance(performance.get("phasesMs"),dict) or len(performance["phasesMs"])>64
                    or any(not isinstance(k,str) or len(k)>128 or type(v) not in (int,float) or not 0 <= v <= 86400000 for k,v in performance["phasesMs"].items())
                    or not isinstance(performance.get("digestCache"),dict) or set(performance["digestCache"]) != {"memoryHits","diskHits","hashReads","bytesHashed"}
                    or any(type(v) is not int or v<0 for v in performance["digestCache"].values())
                    or performance["filePreparationWorkers"]!=2 or performance["replayWorkers"]!=1):
                raise ReplayLabRestoreClientError("restore performance observation is malformed")
            base_fields.add("performance")
        restore_mode = value.get("restoreMode")
        if restore_mode not in (
                "unresolved", "exact-rdc-head", "inventory-sidecar",
                "full-inventory", "project-target-inventory", "no-project-knowledge",
                "verified-rdc-restore", "verified-rdc-subset"):
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")
        target_fields = {"inventoryScope", "fullFrame", "targetLookup"}
        if target_fields & set(value) or (value["completed"] and restore_mode == "project-target-inventory"):
            if restore_mode != "project-target-inventory":
                raise ReplayLabRestoreClientError("restore target scope has an inconsistent mode")
            _validate_restore_target_lookup(value)
            base_fields.update(target_fields)
        if "editing" in value:
            if value["completed"] is not True:
                raise ReplayLabRestoreClientError("uncommitted restore cannot authorize Shader editing")
            base_fields.add("editing")
        try:
            desired_head = _validate_restore_desired_head(
                value.get("desiredHead"))
            inventory_cache = _validate_inventory_cache_summary(
                value.get("inventoryCache"))
            verified_cache = None
            if "verifiedRdcCache" in value:
                verified_cache = _validate_inventory_cache_summary(value["verifiedRdcCache"])
                base_fields.add("verifiedRdcCache")
            receipt_counts = _validate_realized_receipt_summary(
                value.get("realizedReceipts"))
        except ReplayLabRestoreClientError:
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")
        if value["completed"] is False:
            if value.get("outcome") == "action-required":
                try:
                    _bounded_text(
                        value.get("reasonId"), "restore action-required reason", 128
                    )
                except ReplayLabRestoreClientError:
                    raise ReplayLabRestoreClientError(
                        "Bread restore action-required result is invalid"
                    )
                if set(value) != base_fields | {"outcome", "reasonId"}:
                    raise ReplayLabRestoreClientError(
                        "Bread restore completed with an invalid result"
                    )
                return value
            try:
                _bounded_diagnostic(value.get("error"), "restore error")
                _bounded_text(value.get("errorType"), "restore error type", 256)
            except ReplayLabRestoreClientError:
                raise ReplayLabRestoreClientError(
                    "Bread restore failed without a bounded diagnostic")
            if set(value) != base_fields | {"error", "errorType"}:
                raise ReplayLabRestoreClientError(
                    "Bread restore completed with an invalid result")
            return value
        if set(value) != base_fields:
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")
        if restore_mode == "unresolved":
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")

        try:
            if restore_mode == "no-project-knowledge":
                if capture.get("captureId") is not None:
                    raise ReplayLabRestoreClientError(
                        "empty-project restore capture ID must be absent")
            else:
                _hex_text(
                    capture.get("captureId"), SHA256_PATTERN,
                    "restore capture ID")
            _hex_text(
                project.get("workspaceId"), JOB_ID_PATTERN,
                "restore workspace ID")
            _hex_text(
                project.get("replayProjectId"), JOB_ID_PATTERN,
                "restore replay project ID")
        except ReplayLabRestoreClientError:
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")

        cache = value.get("cache")
        shaders = value.get("shaders")
        materials = value.get("materials")
        index = value.get("index")
        warnings = value.get("warnings")
        if (not isinstance(cache, dict) or set(cache) != {"hit", "reason"} or
                not isinstance(cache.get("hit"), bool) or
                not isinstance(shaders, dict) or
                not {"attempted", "applied", "skipped", "failed", "items"} <= set(shaders) or
                not set(shaders) <= {"attempted", "applied", "skipped", "failed", "items",
                                     "supersededLocalEdits"} or
                not isinstance(materials, dict) or
                set(materials) != {"attempted", "restored", "skipped", "failed",
                                    "sessionIds", "items"} or
                not isinstance(index, dict) or
                set(index) != {"read", "present", "written", "warning"} or
                not isinstance(warnings, list) or len(warnings) > MAX_RESULT_ITEMS):
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")
        try:
            _bounded_diagnostic(cache.get("reason"), "restore cache reason")
            shader_counts = {
                field: _bounded_integer(
                    shaders.get(field), "Shader " + field, 0,
                    MAX_PROGRESS_COUNT)
                for field in ("attempted", "applied", "skipped", "failed")
            }
            material_counts = {
                field: _bounded_integer(
                    materials.get(field), "material " + field, 0,
                    MAX_PROGRESS_COUNT)
                for field in ("attempted", "restored", "skipped", "failed")
            }
            for warning in warnings:
                _bounded_diagnostic(warning, "restore warning")
        except ReplayLabRestoreClientError:
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")

        shader_items, shader_statuses = _validate_shader_items(shaders)
        superseded_count = _bounded_integer(
            shaders.get("supersededLocalEdits", 0), "Shader preserved-source backup count",
            0, MAX_PROGRESS_COUNT)
        if superseded_count != shader_statuses.get("supersededLocalEdits", 0):
            raise ReplayLabRestoreClientError("Bread restore Shader backup count is inconsistent")
        verified_hit = restore_mode == "verified-rdc-restore"
        verified_subset = restore_mode == "verified-rdc-subset"
        verified_written = verified_cache is not None and verified_cache["written"]
        pending_source_count = (_validated_source_only_skips(shader_items)
                                if verified_hit or verified_written else 0)
        if (receipt_counts["reusedValidation"] != shader_statuses["reusedValidation"] or
                receipt_counts["manualEdit"] != shader_statuses["manualEdit"] or
                (receipt_counts["manualEdit"] and (verified_hit or index["written"] or
                    receipt_counts["persisted"] > shader_counts["applied"] - receipt_counts["manualEdit"])) or
                (verified_hit and (
                    verified_cache != {"hit": True, "written": False,
                                       "reason": "same-rdc-verified-content"} or
                    cache != {"hit": True, "reason": "same-rdc-verified-content"} or
                    shader_counts["applied"] <= 0 or
                    shader_counts["applied"] != shader_counts["attempted"] or
                    receipt_counts["reusedValidation"] != shader_counts["applied"] or
                    any(item["source"] != "verified-rdc-restore" for item in shader_items
                        if item["status"] == "applied") or
                    shader_counts["skipped"] != pending_source_count or shader_counts["failed"] or
                    any(material_counts.values()) or
                    receipt_counts["loadedFromIndex"] or receipt_counts["persisted"] or
                    inventory_cache["hit"] or inventory_cache["written"] or
                    index != {"read": False, "present": False,
                              "written": False, "warning": None})) or
                (verified_subset and (
                    verified_cache != {"hit": True, "written": False, "reason": "same-rdc-verified-subset"}
                    or cache != {"hit": True, "reason": "same-rdc-verified-subset"}
                    or not any(item.get("source") == "verified-rdc-restore" for item in shader_items)
                    or verified_written or index["written"] or receipt_counts["persisted"])) or
                (not verified_hit and not verified_subset and verified_cache is not None and verified_cache["hit"]) or
                (verified_written and (
                    verified_cache["reason"] != ("validated-content-subset-snapshot"
                        if receipt_counts["manualEdit"] else "validated-content-snapshot") or
                    index["written"] or index["warning"] is not None or
                    shader_counts["applied"] <= 0 or shader_counts["skipped"] != pending_source_count or
                    shader_counts["failed"] or material_counts["attempted"] or
                    receipt_counts["persisted"] != shader_counts["applied"] - receipt_counts["manualEdit"] or
                    receipt_counts["persisted"] <= 0))):
            raise ReplayLabRestoreClientError(
                "Bread restore verified RDC cache evidence is inconsistent")
        if "editing" in value:
            _validate_restore_editing(value["editing"], shader_items, capture_path, capture_session_id)
        if restore_mode == "project-target-inventory" and inventory_cache["written"]:
            raise ReplayLabRestoreClientError("target Shader lookup cannot publish a full inventory cache")
        material_items, material_statuses, restored_sessions = (
            _validate_material_items(materials))
        session_ids = materials.get("sessionIds")
        if (not isinstance(session_ids, list) or
                len(session_ids) != material_counts["restored"]):
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")
        try:
            clean_sessions = [
                _hex_text(item, JOB_ID_PATTERN, "material session ID")
                for item in session_ids
            ]
        except ReplayLabRestoreClientError:
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")
        if (shader_counts["attempted"] !=
                shader_counts["applied"] + shader_counts["failed"] or
                len(shader_items) !=
                shader_counts["attempted"] + shader_counts["skipped"] or
                any(shader_statuses[field] != shader_counts[field]
                    for field in ("applied", "skipped", "failed")) or
                material_counts["attempted"] !=
                material_counts["restored"] + material_counts["failed"] or
                len(material_items) !=
                material_counts["attempted"] + material_counts["skipped"] or
                any(material_statuses[field] != material_counts[field]
                    for field in ("restored", "skipped", "failed")) or
                clean_sessions != restored_sessions):
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")

        if (not all(isinstance(index.get(field), bool)
                    for field in ("read", "present", "written")) or
                (index.get("warning") is not None and
                 not isinstance(index.get("warning"), str)) or
                (index["present"] and not index["read"]) or
                (index["written"] and
                 (not index["read"] or shader_counts["applied"] <= 0 or
                  index.get("warning") is not None)) or
                receipt_counts["produced"] != shader_counts["applied"] or
                receipt_counts["adoptedCurrentSession"] !=
                shader_statuses["adoptedCurrentSession"] or
                (index["written"] and
                 (desired_head is None or
                  receipt_counts["persisted"] !=
                  receipt_counts["produced"])) or
                (not index["written"] and not verified_written and
                 receipt_counts["persisted"] != 0) or
                (cache["hit"] and not verified_hit and not verified_subset and
                 (cache["reason"] != "exact-desired-project-head" or
                  restore_mode != "exact-rdc-head" or
                  desired_head is None or
                  receipt_counts["loadedFromIndex"] <= 0 or
                  receipt_counts["loadedFromIndex"] !=
                  receipt_counts["validatedForRouting"] or
                  shader_counts["attempted"] <= 0 or
                  any(item.get("source") != "rdc-index"
                      for item in shader_items))) or
                (not cache["hit"] and restore_mode in ("exact-rdc-head", "verified-rdc-restore")) or
                (inventory_cache["hit"] and
                  restore_mode != "inventory-sidecar") or
                (restore_mode == "inventory-sidecar" and
                  not inventory_cache["hit"]) or
                (restore_mode == "no-project-knowledge" and (
                    desired_head is not None or
                    cache != {
                        "hit": False,
                        "reason": "project-has-no-restorable-knowledge",
                    } or
                    inventory_cache != {
                        "hit": False,
                        "written": False,
                        "reason": "not-required-for-empty-project",
                    } or
                    any(shader_counts.values()) or
                    any(material_counts.values()) or
                    any(receipt_counts.values()) or
                    bool(shader_items) or bool(material_items) or
                    bool(clean_sessions) or bool(warnings) or
                    index != {
                        "read": False,
                        "present": False,
                        "written": False,
                        "warning": None,
                    }))):
            raise ReplayLabRestoreClientError(
                "Bread restore completed with an invalid result")
        if index.get("warning") is not None:
            try:
                _bounded_diagnostic(
                    index["warning"], "restore index warning")
            except ReplayLabRestoreClientError:
                raise ReplayLabRestoreClientError(
                    "Bread restore completed with an invalid result")
        return value

    def restore(self, project_root, capture_path, capture_session_id,
                progress_callback=None):
        with _RESTORE_LIFECYCLE:
            if _RESTORE_STOPPING:
                raise ReplayLabRestoreClientError(
                    "Bread project restore was cancelled during shutdown")
            self._restore_generation = _RESTORE_GENERATION
            _ACTIVE_CLIENTS.add(self)
        try:
            return self._restore(
                project_root, capture_path, capture_session_id,
                progress_callback)
        finally:
            with _RESTORE_LIFECYCLE:
                _ACTIVE_CLIENTS.discard(self)
                _RESTORE_LIFECYCLE.notify_all()

    def _restore(self, project_root, capture_path, capture_session_id,
                 progress_callback=None):
        if progress_callback is not None and not callable(progress_callback):
            raise ReplayLabRestoreClientError(
                "progress callback must be callable")
        _publish_progress(progress_callback, "client-validate")
        project = _clean_path(project_root, "project root")
        capture = _clean_path(capture_path, "capture path", require_rdc=True)
        session = str(capture_session_id or "").strip().lower()
        if SESSION_PATTERN.fullmatch(session) is None:
            raise ReplayLabRestoreClientError(
                "capture session ID is invalid")

        self._check_cancelled()
        startup_deadline = self.monotonic() + self.startup_timeout_seconds
        _publish_progress(progress_callback, "host-state")
        port, token, pid, version, host_instance_id = self._credentials(
            startup_deadline)
        _publish_progress(progress_callback, "host-health")
        self._verify_health(port, pid, version, host_instance_id)
        _publish_progress(progress_callback, "restore-submit")
        self._check_cancelled()
        self.replay_inspection_safe = not self._confirm_replay_rollback
        started = self._request(port, token, "POST", RESTORE_JOB_PATH, {
            "projectRoot": project,
            "capturePath": capture,
            "captureSessionId": session,
        }, cancellation_cleanup=self._confirm_replay_rollback)
        job_id, status, events, latest_sequence = self._snapshot(
            started, after_sequence=0)
        sequence = 0
        for event in events:
            sequence = event["sequence"]
            _publish_progress(
                progress_callback, event["phase"], event["current"],
                event["total"], event["message"], event["item"])
        if not events:
            if status == "queued":
                _publish_progress(progress_callback, "restore-queued")
            elif status == "running":
                _publish_progress(progress_callback, "restore-running")
        last_status = status
        if status in ("completed", "failed") and sequence >= latest_sequence:
            if status == "completed":
                _publish_progress(progress_callback, "result-validate")
                value = self._result(
                    started.get("result"), project, capture, session)
                self.replay_inspection_safe = True
                return value
            error = str(started.get("error", "") or "").strip()
            if error == "Bread project restore was cancelled":
                self.replay_inspection_safe = True
            raise ReplayLabRestoreClientError(
                error[:4096] or "Bread project restore failed")

        deadline = self.monotonic() + self.timeout_seconds
        try:
            while self.monotonic() < deadline:
                self._check_cancelled()
                # job_id is already a strictly validated lowercase hexadecimal
                # token, so it is safe to place in this fixed local URL path.
                query = "%s/%s?after=%d" % (
                    RESTORE_JOB_PATH, job_id, sequence)
                try:
                    snapshot = self._request(port, token, "GET", query)
                except ReplayLabRestoreTransportError as exc:
                    # A short synchronous WinHTTP receive slice can expire
                    # while the owned background job is still progressing
                    # (qrenderdoc's own Python thread is busy serving the
                    # restore's replay work, so this poll thread runs late).
                    # Re-read only this known job; never re-submit a mutation.
                    if (exc.winerror != 12002 or exc.method != "GET"
                            or exc.path != query):
                        raise
                    self._check_cancelled()
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        break
                    self._wait(min(max(self.poll_seconds, 1.0), remaining))
                    continue
                current_job_id, status, events, latest_sequence = self._snapshot(
                    snapshot, job_id, sequence)
                del current_job_id
                for event in events:
                    sequence = event["sequence"]
                    _publish_progress(
                        progress_callback, event["phase"], event["current"],
                        event["total"], event["message"], event["item"])
                if not events and status != last_status:
                    if status == "queued":
                        _publish_progress(progress_callback, "restore-queued")
                    elif status == "running":
                        _publish_progress(progress_callback, "restore-running")
                last_status = status
                # A terminal snapshot can still contain only the next page from a
                # bounded progress ring. Drain every available newer page before
                # handing the final result to the dialog.
                if (status in ("completed", "failed") and
                        sequence < latest_sequence):
                    continue
                if status == "completed":
                    _publish_progress(progress_callback, "result-validate")
                    value = self._result(
                        snapshot.get("result"), project, capture, session)
                    self.replay_inspection_safe = True
                    return value
                if status == "failed":
                    error = str(snapshot.get("error", "") or "").strip()
                    if error == "Bread project restore was cancelled":
                        self.replay_inspection_safe = True
                    raise ReplayLabRestoreClientError(
                        error[:4096] or "Bread project restore failed")
                self._wait(self.poll_seconds)
        except ReplayLabRestoreClientError:
            if self._inspection_requested.is_set() and not self.replay_inspection_safe:
                self._confirm_restore_rollback(port, token, job_id)
            elif self._cancel_requested():
                self._cancel_restore_job_best_effort(port, token, job_id)
            raise
        if self._inspection_requested.is_set():
            self._confirm_restore_rollback(port, token, job_id)
        else:
            self._cancel_restore_job_best_effort(port, token, job_id)
        raise ReplayLabRestoreClientError(
            "Bread project restore timed out")


class ReplayLabInternalProjectClient(ReplayLabRestoreHostClient):
    """Invoke one typed ProjectService operation over the shared Host."""

    def __init__(self, internal_executable, runtime_directory=None,
                 startup_timeout_seconds=60, request_timeout_seconds=30,
                 transport=None, health_transport=None,
                 monotonic=None, sleep=None):
        # A cold one-file deployment starts an ``ensure`` controller and then
        # the long-lived service, so two bounded extraction/verification phases
        # can legitimately exceed 15 seconds on a freshly built Windows host.
        ReplayLabRestoreHostClient.__init__(
            self,
            runtime_directory=runtime_directory,
            timeout_seconds=max(1, request_timeout_seconds),
            startup_timeout_seconds=startup_timeout_seconds,
            poll_seconds=0.1,
            request_timeout_seconds=min(4, request_timeout_seconds),
            transport=transport,
            health_transport=health_transport,
            sleep=sleep,
            monotonic=monotonic,
            internal_executable=internal_executable,
        )
        # Project creation may validate/hash a bounded capture list. It runs on
        # the launcher's cancellable worker rather than the restore submit/poll
        # path, so retain the established finite 90-second operation budget.
        self.request_timeout_seconds = min(
            90.0, max(0.1, float(request_timeout_seconds)))
        if transport is None and self.internal_executable:
            self.transport = self._project_transport

    def _project_transport(self, port, token, method, path, body, timeout_seconds,
                           cancel_event, expected_generation):
        if method != "POST" or path != PROJECT_OPERATION_PATH:
            raise ReplayLabRestoreClientError("Invalid private project request")
        deadline=self.monotonic()+min(90.0,max(0.1,float(timeout_seconds)))
        self._check_cancelled()
        packet=json.dumps(dict(port=int(port),token=token,method=method,path=path,
            body=base64.b64encode(body).decode("ascii"),timeout=min(90.0,max(0.1,float(timeout_seconds))))).encode("utf-8")
        environment=os.environ.copy();environment["PYINSTALLER_RESET_ENVIRONMENT"]="1"
        child=subprocess.Popen([self.internal_executable,"project-request"],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=os.path.dirname(self.internal_executable),
            env=environment,creationflags=int(getattr(subprocess,"CREATE_NO_WINDOW",0x08000000)) if os.name=="nt" else 0)
        completed=threading.Event();received={}
        def exchange():
            try:received["output"],received["error"]=child.communicate(input=packet)
            except Exception as error:received["failure"]=error
            finally:completed.set()
        worker=threading.Thread(target=exchange);worker.daemon=True;worker.start()
        try:
            while not completed.wait(0.05):
                self._check_cancelled()
                if cancel_event.is_set():raise ReplayLabRestoreClientError("Bread project request was cancelled")
                if self.monotonic()>=deadline:raise ReplayLabRestoreRequestUncertain("Bread project request timed out")
            self._check_cancelled()
            if "failure" in received:raise ReplayLabRestoreRequestUncertain("Bread project helper communication failed")
            output=received.get("output",b"")
            if child.returncode!=0 or len(output)>4*((MAX_RESPONSE_BYTES+2)//3)+4096:
                raise ReplayLabRestoreRequestUncertain("Bread project request helper failed")
            result=_strict_json_bytes(output,"Bread project helper")
            if result.get("error"):
                raise ReplayLabRestoreRequestUncertain("Bread project request outcome is uncertain: %s" % str(result["error"])[:1024])
            return int(result["status"]),base64.b64decode(result["body"],validate=True)
        finally:
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=1.0)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=1.0)
            worker.join(timeout=1.0)

    def _ensure_host_when_missing(self, deadline):
        """Recover a lost service on the project worker, without restarting the GUI or AI."""
        self._check_cancelled()
        if not self.internal_executable:
            return
        state_path = os.path.join(self.runtime_directory, "internal-host.json")
        if os.path.isfile(state_path):
            try:
                state = _strict_json_bytes(
                    _read_bounded(state_path, MAX_STATE_BYTES, "Bread internal Host state"),
                    "Bread internal Host state")
            except ReplayLabRestoreClientError:
                # Preserve malformed evidence for the ordinary credential diagnostic.
                return
            if _process_alive(state.get("pid")):
                # Never launch over a live but incompatible or unresponsive service.
                return
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)) if os.name == "nt" else 0
        environment = os.environ.copy()
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        try:
            controller = subprocess.Popen(
                [self.internal_executable, "ensure", "--runtime-dir", self.runtime_directory,
                 "--owner-pid", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=os.path.dirname(self.internal_executable), env=environment,
                creationflags=flags, shell=False)
        except OSError as error:
            raise ReplayLabRestoreClientError(
                "Bread internal Host could not be started: %s" % str(error))
        try:
            while controller.poll() is None:
                if self.monotonic() >= deadline:
                    raise ReplayLabRestoreClientError("Bread internal Host startup timed out")
                self._wait(min(self.poll_seconds, 0.1))
            self._check_cancelled()
            if controller.returncode != 0:
                raise ReplayLabRestoreClientError(
                    "Bread internal Host startup failed (exit %s)" % controller.returncode)
        finally:
            if controller.poll() is None:
                # Only this short-lived controller is owned here, never its detached service.
                controller.terminate()
                try:
                    controller.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    controller.kill()
                    controller.wait(timeout=1.0)

    def invoke(self, operation, parameters, request_id=None):
        with _RESTORE_LIFECYCLE:
            if _RESTORE_STOPPING:
                raise ReplayLabRestoreClientError(
                    "Bread internal project request was cancelled during shutdown")
            self._restore_generation = _RESTORE_GENERATION
            _ACTIVE_CLIENTS.add(self)
        try:
            return self._invoke(operation, parameters, request_id)
        finally:
            with _RESTORE_LIFECYCLE:
                _ACTIVE_CLIENTS.discard(self)
                _RESTORE_LIFECYCLE.notify_all()

    def _invoke(self, operation, parameters, request_id=None):
        name = str(operation or "").strip()
        if not name or len(name.encode("utf-8")) > 64:
            raise ReplayLabRestoreClientError(
                "Bread internal project operation is invalid")
        if not isinstance(parameters, dict):
            raise ReplayLabRestoreClientError(
                "Bread internal project parameters must be an object")
        identity = str(request_id or uuid.uuid4().hex).strip().lower()
        if JOB_ID_PATTERN.fullmatch(identity) is None:
            raise ReplayLabRestoreClientError(
                "Bread internal project request ID is invalid")
        deadline = self.monotonic() + self.startup_timeout_seconds
        self._ensure_host_when_missing(deadline)
        port, token, pid, version, host_instance_id = self._credentials(deadline)
        self._verify_health(port, pid, version, host_instance_id)
        value = self._request(
            port,
            token,
            "POST",
            PROJECT_OPERATION_PATH,
            {
                "schema": PROJECT_OPERATION_SCHEMA,
                "requestId": identity,
                "operation": name,
                "parameters": parameters,
            },
            maximum_request_bytes=MAX_PROJECT_REQUEST_BYTES,
        )
        if (not isinstance(value, dict) or
                value.get("schema") != PROJECT_OPERATION_RESULT_SCHEMA or
                value.get("requestId") != identity or
                set(value) != {"schema", "requestId", "result"} or
                not isinstance(value.get("result"), dict)):
            raise ReplayLabRestoreClientError(
                "Bread internal project response is invalid")
        return value["result"]


__all__ = [
    "DEFAULT_PROJECT_RESTORE_CLIENT_TIMEOUT_SECONDS",
    "EXPECTED_HOST_VERSION",
    "EXPECTED_INTERNAL_CAPABILITY_VERSIONS",
    "INTERNAL_HOST_CAPABILITY_SCHEMA",
    "INTERNAL_HOST_STATE_PROTOCOL_VERSION",
    "PROJECT_RESTORE_CLEANUP_GRACE_SECONDS",
    "PROJECT_RESTORE_DEADLINE_SECONDS",
    "PROJECT_RESTORE_MAX_INFLIGHT_SECONDS",
    "PROJECT_OPERATION_RESULT_SCHEMA",
    "PROJECT_OPERATION_SCHEMA",
    "RESTORE_JOB_SCHEMA",
    "ReplayLabInternalProjectClient",
    "ReplayLabRestoreClientError",
    "ReplayLabRestoreHostIncompatible",
    "ReplayLabRestoreHostClient",
    "ReplayLabRestoreRequestUncertain",
    "ReplayLabRestoreTransportError",
    "begin_restore_shutdown",
    "cancel_active_restores",
    "resolve_internal_executable",
    "resume_restore_generation",
    "runtime_root",
    "wait_restore_quiescent",
]
